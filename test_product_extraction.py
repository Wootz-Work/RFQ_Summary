"""
test_product_extraction.py
──────────────────────────
Tests the product-extraction pipeline (prompt v3) without hitting Claude or Glide:

  1. NDJSON parses into typed products and separate query objects
  2. lenient parsing survives fences, pretty-printed objects and junk lines
  3. rows with no name never reach the product table
  4. the prompt rules that are checked in code produce warnings, not silence
  5. the Glide payload carries the right column ids, types and defaults
  6. query rows link to the Row IDs Glide returns for their products

Run:
    python test_product_extraction.py
"""

import json
import sys

sys.path.insert(0, "src")

import httpx

from rfq_summary.config import Settings
from rfq_summary.glide_client import glide_add_product_rows, glide_add_query_rows
from rfq_summary.product_extraction import parse_product_extraction
from rfq_summary.schema import ExtractedProduct, ExtractedQuery

DETAILS = (
    "Specification:\n`M10 X 1.5 X 25MM HEX GR 8.8`\nHexagon head cap screw, fully threaded.\n"
    "Carbon or alloy steel, property class 8.8.\n<br>\nScope:\n"
    "Manufacture, heat treatment, coating, inspection, certification.\n<br>\n"
    "Application:\n\\--\n<br>\nApplicable standards:\nISO 4017:2022 — dimensions (attached)\n<br>\n"
    "Additional note:\nQuote each tier separately."
)

INTERNAL = (
    "Sourcing: Cold heading + thread rolling; Cr(VI)-free passivation line.\n"
    "Assumptions: MTL5102A treated as applicable at class 8.8, its upper limit.\n"
    "Context: Price-conscious, competing on volume."
)

NDJSON = "\n".join(
    [
        json.dumps(
            {
                "type": "rfq_header",
                "project": "Project Falcon",
                "rfq_title": "Fasteners and washers",
                "line_count_expected": 3,
                "line_count_extracted": 3,
                "reconciliation": "3 items in the email, all parsed",
                "common_conditions": "Certificates with every shipment, all lines.",
            }
        ),
        json.dumps(
            {
                "type": "product",
                "index": 1,
                "source_ref": "Item 1",
                "name": "Hex Cap Screw M10 x 25 — 8.8",
                "structure": "single",
                "variant_count": None,
                "quantity": "160,000 / 325,000 / 650,000 pcs",
                "quantity_basis": "price_breaks",
                "details": DETAILS,
                "internal_notes": INTERNAL,
                "target_price": None,
                "dwg_link": "https://example.com/dwg/iso4017.pdf",
                "rep_url": None,
                "addl_files": [],
                "annexure": None,
                "provenance": {"name": "derived", "application": "unknown"},
            }
        ),
        # Family line, with its own line-specific query following it.
        json.dumps(
            {
                "type": "product",
                "index": 2,
                "source_ref": "Items 2-15",
                "name": "Flat Washers — 14 sizes (family)",
                "structure": "family",
                "variant_count": 14,
                "quantity": "As per annexure",
                "quantity_basis": "annual",
                "details": DETAILS,
                "internal_notes": INTERNAL,
                "target_price": "$2.68 - FOB India",
                "dwg_link": None,
                "rep_url": "https://example.com/catalogue",
                "addl_files": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                "annexure": {"required": True, "by_reference": True, "suggested_filename": "sizes.xlsx"},
                "provenance": {"name": "verbatim", "target_price": "verbatim"},
            }
        ),
        json.dumps(
            {
                "type": "query",
                "query_ref": "Q1",
                "product_ref": [2],
                "section": "specification",
                "description": "DIN 125 offers 140 HV and 200 HV. We would suggest 140 HV against class 8.8 bolts — please confirm.",
                "photo": [],
            }
        ),
        # Pretty-printed product spanning several lines, quantity as a bare number.
        json.dumps(
            {
                "type": "product",
                "index": 3,
                "source_ref": "email body",
                "name": "Sodium Hypochlorite System",
                "structure": "system",
                "quantity": 4,
                "details": DETAILS,
                "internal_notes": "Sourcing: Process-skid fabricator.",
            },
            indent=2,
        ),
        # Scratch row: no name, must never be emitted.
        json.dumps({"type": "product", "index": 4, "name": "", "details": ""}),
        # RFQ-level query covering the Application placeholder on every line.
        json.dumps(
            {
                "type": "query",
                "query_ref": "Q2",
                "product_ref": None,
                "section": "application",
                "description": "What is the end application for these parts? It lets us propose equivalents where they would save cost.",
                "photo": [],
            }
        ),
        json.dumps(
            {
                "type": "rfq_summary",
                "placeholder_count": 3,
                "query_count": 2,
                "notes_for_reviewer": "Currency and incoterm still open.",
            }
        ),
    ]
)

FENCED = "Here is the output:\n```json\n" + NDJSON + "\n```\n"


def _check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


class _StubHttp:
    """Stands in for httpx.Client, capturing payloads and replaying Row IDs."""

    def __init__(self, row_ids=None):
        self.sent = []
        self._row_ids = row_ids or []
        self._cursor = 0

    def install(self):
        self._real = httpx.Client
        stub = self

        class _Resp:
            def __init__(self, body):
                self._body = body

            def raise_for_status(self):
                pass

            def json(self):
                return self._body

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                stub.sent.append(json)
                n = len(json.get("mutations", []))
                body = [{"Row ID": rid} for rid in stub._row_ids[stub._cursor : stub._cursor + n]]
                stub._cursor += n
                return _Resp(body)

        httpx.Client = _Client
        return self

    def restore(self):
        httpx.Client = self._real


def test_parse() -> bool:
    ok = True
    for label, text in (("plain NDJSON", NDJSON), ("fenced + prose", FENCED)):
        r = parse_product_extraction(text)

        ok &= _check(f"[{label}] no parse errors", not r.parse_errors, str(r.parse_errors))
        ok &= _check(f"[{label}] header uses project, not customer", r.header.project == "Project Falcon")
        ok &= _check(f"[{label}] common_conditions captured", "every shipment" in r.header.common_conditions)
        ok &= _check(f"[{label}] 3 products emitted", len(r.products) == 3, f"got {len(r.products)}")
        ok &= _check(f"[{label}] 2 queries emitted", len(r.queries) == 2, f"got {len(r.queries)}")
        ok &= _check(f"[{label}] unnamed row skipped", len(r.skipped_products) == 1)
        ok &= _check(f"[{label}] counts reconcile", r.reconciliation_note() == "", r.reconciliation_note())

        screw, family, system = r.products
        ok &= _check(f"[{label}] order preserved", [p.index for p in r.products] == [1, 2, 3])
        ok &= _check(f"[{label}] qty is a plain string", screw.quantity == "160,000 / 325,000 / 650,000 pcs")
        ok &= _check(f"[{label}] quantity_basis kept", screw.quantity_basis == "price_breaks")
        ok &= _check(f"[{label}] numeric qty coerced", system.quantity == "4", system.quantity)
        ok &= _check(f"[{label}] internal notes captured", "Sourcing:" in screw.internal_notes)
        ok &= _check(f"[{label}] target price verbatim", family.target_price == "$2.68 - FOB India")
        ok &= _check(f"[{label}] absent target price is None", screw.target_price is None)
        ok &= _check(f"[{label}] annexure by reference", bool(family.annexure and family.annexure.by_reference))
        ok &= _check(f"[{label}] placeholder counted", screw.placeholder_count() == 1)

        ok &= _check(f"[{label}] line query linked to its line", r.queries_for(2)[0].query_ref == "Q1")
        ok &= _check(f"[{label}] query reads as the team would ask it", r.queries_for(2)[0].description.startswith("DIN 125 offers"))
        ok &= _check(f"[{label}] rfq-level query has no product_ref", r.rfq_level_queries()[0].query_ref == "Q2")
        ok &= _check(f"[{label}] no validation warnings", not r.validation_warnings, str(r.validation_warnings))
    return ok


def test_validations() -> bool:
    """The prompt rules the maintainer notes ask to enforce in code, not prose."""
    bad = "\n".join(
        [
            json.dumps(
                {
                    "type": "product",
                    "index": 1,
                    "name": "M10 x 1.5 x 25mm Hex Head Cap Screw ISO 4017 — Grade 8.8 Steel, MTL5102A",
                    "quantity": "8,000 pcs",
                    "details": "Specification:\n**Summary:** a bold sub-heading\n<br>\nScope:\n\\--",
                    "provenance": {"specification": "verbatim + derived (cross-referenced)"},
                }
            ),
            json.dumps({"type": "query", "query_ref": "Q1", "product_ref": [1], "section": "scope",
                        "description": "Confirm the scope boundary."}),
            json.dumps({"type": "query", "query_ref": "Q2", "product_ref": [1], "section": "scope",
                        "description": "confirm the scope boundary."}),
            json.dumps({"type": "query", "query_ref": "Q3", "product_ref": [9], "section": "quantity",
                        "description": "Is this annual? And what incoterm applies?"}),
            json.dumps({"type": "query", "query_ref": "Q4", "product_ref": [1], "section": "pricing",
                        "description": "What currency?"}),
            json.dumps({"type": "rfq_summary", "placeholder_count": 5, "query_count": 2}),
        ]
    )
    r = parse_product_extraction(bad)
    w = " || ".join(r.validation_warnings)

    ok = _check("over-long name flagged", "max 50" in w, w)
    ok &= _check("provenance phrase flagged", "single token" in w, w)
    ok &= _check("bold sub-heading flagged", "bold sub-heading" in w, w)
    ok &= _check("placeholder count mismatch flagged", "placeholder_count says 5" in w, w)
    ok &= _check("query count mismatch flagged", "query_count says 2" in w, w)
    ok &= _check("duplicate query flagged", "duplicate query text" in w, w)
    ok &= _check("two-questions-in-one flagged", "more than one question" in w, w)
    ok &= _check("unknown section flagged", "unknown section" in w, w)
    ok &= _check("query pointing at a missing line flagged", "not extracted" in w, w)

    # A response is the customer's to give; never accept one from the model.
    q = ExtractedQuery.model_validate({"description": "x?", "Query Response": "B1", "response": "B1"})
    ok &= _check("model-supplied response discarded", not hasattr(q, "response"))
    return ok


def test_banned_queries() -> bool:
    """§1.2 — four kinds of question that must never reach a customer."""
    banned = [
        ("a file we could not open", "One of the attached files would not open at our end — could you resend the drawing?"),
        ("an unreadable file", "The PDF appears corrupt — please share the file again."),
        ("our own tracking", "Could you share your project reference number for our internal records?"),
        ("the supplier model", "Which hardness class applies, so our supplier can quote correctly?"),
        ("quantity basis", "Are these quantities annual usage or a one-time requirement?"),
        ("quantity basis", "Is the 650,000 volume a one-time order or recurring?"),
        ("a commercial term", "What currency and incoterm should we quote against?"),
        ("a project name", "No project name was provided. Could you confirm the project or programme name?"),
        ("a PPAP ask", "Is PPAP required on these parts, and if so at which level?"),
        ("a fetch failure", "Two of the attached files failed to fetch — could you check and resend them?"),
    ]
    allowed = [
        "MTL5102B has two sub-states: B1 (min 5 um, 480 h salt spray) and B2 (min 8 um, 720 h). Which applies?",
        "DIN 125 offers 140 HV and 200 HV. We would suggest 140 HV against class 8.8 bolts — please confirm.",
        "Is any of these parts used in a safety-critical assembly? It changes the inspection level we build in.",
        "Drawing MT-4471 rev C is referenced but we have rev B. Which revision should we quote against?",
        "The enquiry references ISO 4014 but it was not attached. Could you share a copy?",
        # Real technical questions from the live run — these must survive the filter.
        "The descriptor references VDA235-104 without the .20 suffix used elsewhere. We have treated it as "
        "VDA 235-104.20. Please confirm, or clarify if a different VDA code applies.",
        "What is the end application for these fasteners? Knowing the assembly helps us propose equivalents "
        "where they would save cost, and set the right inspection level if any are safety-critical.",
    ]

    def warnings_for(text):
        nd = "\n".join([
            json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10", "details": "Specification:\nx\n\\--"}),
            json.dumps({"type": "query", "query_ref": "Q1", "product_ref": [1],
                        "section": "specification", "description": text}),
        ])
        return [w for w in parse_product_extraction(nd).validation_warnings if "query Q1" in w]

    # A commercial section is itself the defect, whatever the wording.
    nd = "\n".join([
        json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10", "details": "Specification:\nx\n\\--"}),
        json.dumps({"type": "query", "query_ref": "Q1", "product_ref": [1],
                    "section": "commercial", "description": "What are the payment milestones?"}),
    ])
    ok = _check(
        "flags a commercial section",
        any("commercial query" in w for w in parse_product_extraction(nd).validation_warnings),
    )
    for label, text in banned:
        ok &= _check(f"flags {label}", bool(warnings_for(text)), text[:60])
    for text in allowed:
        ok &= _check(f"allows {text[:44]!r}", not warnings_for(text), str(warnings_for(text)))
    return ok


def test_query_budget_and_merging() -> bool:
    """§1.2 — four questions per RFQ, and one question covering several lines is one row."""
    def build(queries):
        objs = [{"type": "product", "index": i, "name": f"Part {i}",
                 "details": "Specification:\nx\n\\--"} for i in (1, 2, 3, 4)]
        return "\n".join(json.dumps(o) for o in objs + queries)

    same_a = ("The descriptor references both MTL5102A and VDA 235-104.20. We have treated "
              "VDA 235-104.20 as the VDA code for the same coating defined in MTL5102A. Please confirm.")
    same_b = ("The descriptor references VDA235-104 without the .20 suffix. We have treated this "
              "as the same VDA 235-104.20 / MTL5102A coating. Please confirm.")
    other = "DIN 125 Part 1 offers two hardness classes: 140 HV and 200 HV. Should we quote 140 HV?"
    third = "MTL5102B has two sub-states: B1 (min 5 um, 480 h) and B2 (min 8 um, 720 h). Which applies?"

    # The same question split across two lines must be caught even though the wording differs.
    r = parse_product_extraction(build([
        {"type": "query", "query_ref": "Q1", "product_ref": 1, "section": "specification", "description": same_a},
        {"type": "query", "query_ref": "Q8", "product_ref": 4, "section": "specification", "description": same_b},
    ]))
    ok = _check("reworded duplicate caught", any("ask the same thing" in w for w in r.validation_warnings),
                str(r.validation_warnings))
    ok &= _check("merge hint names both lines", any("[1, 4]" in w for w in r.validation_warnings))

    # Genuinely different questions in the same section must not be merged.
    r = parse_product_extraction(build([
        {"type": "query", "query_ref": "Q3", "product_ref": 2, "section": "specification", "description": other},
        {"type": "query", "query_ref": "Q5", "product_ref": 3, "section": "specification", "description": third},
    ]))
    ok &= _check("distinct questions left alone", not any("ask the same thing" in w for w in r.validation_warnings),
                 str(r.validation_warnings))

    # Five questions is over the cap.
    five = [{"type": "query", "query_ref": f"Q{i}", "product_ref": [i % 4 + 1], "section": "standards",
             "description": f"Drawing rev {i} conflicts with the enquiry. Which revision governs part {i}?"}
            for i in range(5)]
    r = parse_product_extraction(build(five))
    ok &= _check("over-cap flagged", any("the cap is 4" in w for w in r.validation_warnings))

    # Four is fine, and one row may carry several lines.
    r = parse_product_extraction(build([
        {"type": "query", "query_ref": "Q1", "product_ref": [1, 4], "section": "specification", "description": same_a},
        {"type": "query", "query_ref": "Q3", "product_ref": [2], "section": "specification", "description": other},
        {"type": "query", "query_ref": "Q5", "product_ref": [3], "section": "specification", "description": third},
    ]))
    ok &= _check("at-cap run is clean", not r.validation_warnings, str(r.validation_warnings))
    ok &= _check("merged query covers both lines", r.queries[0].product_refs == [1, 4])
    ok &= _check("queries_for finds a merged query", r.queries_for(4)[0].query_ref == "Q1")
    return ok


def test_truncation_names_the_lost_line() -> bool:
    """A real 9-line RFQ truncated mid-way through the ninth product."""
    objs = [{"type": "rfq_header", "project": "P", "line_count_expected": 9}]
    objs += [{"type": "product", "index": i, "name": f"Line {i}", "details": "Specification:\nx"}
             for i in range(1, 9)]
    text = "\n".join(json.dumps(o) for o in objs)
    text += ('\n{"type":"product","index":9,"source_ref":"M080726 / Supports",'
             '"name":"Pipe Supports — Phase 2.5 BoP (family)","structure":"family",'
             '"variant_count":42,"quantity"')

    r = parse_product_extraction(text)
    truncation = [e for e in r.parse_errors if "truncated" in e]

    ok = _check("eight complete lines still parse", len(r.products) == 8, str(len(r.products)))
    ok &= _check("truncation is reported", bool(truncation), str(r.parse_errors))
    ok &= _check("the lost line is named", "line 9" in (truncation[0] if truncation else ""))
    ok &= _check("its name is named", "Pipe Supports" in (truncation[0] if truncation else ""))
    ok &= _check("count mismatch surfaces too",
                 r.reconciliation_note() == "line_count_expected=9 but 8 product line(s) parsed")
    # A half-specified row is worse than a missing one: nothing partial is emitted.
    ok &= _check("no partial row emitted", all(p.index != 9 for p in r.products))

    # Ordinary junk is not mistaken for truncation.
    r2 = parse_product_extraction("I could not find any products in this email.")
    ok &= _check("prose is not called truncation", not [e for e in r2.parse_errors if "truncated" in e])
    return ok


def test_mismatch_is_reported() -> bool:
    text = "\n".join(
        [
            json.dumps({"type": "rfq_header", "line_count_expected": 5}),
            json.dumps({"type": "product", "index": 1, "name": "Hex Bolt M10 x 120", "details": DETAILS}),
        ]
    )
    r = parse_product_extraction(text)
    return _check(
        "count mismatch surfaced",
        r.reconciliation_note() == "line_count_expected=5 but 1 product line(s) parsed",
        r.reconciliation_note(),
    )


def test_garbage_is_not_fatal() -> bool:
    r = parse_product_extraction("I could not find any products in this email.")
    ok = _check("prose-only output yields no products", not r.products)
    ok &= _check("prose-only output records an error", bool(r.parse_errors))
    ok &= _check("empty output yields no products", not parse_product_extraction("").products)
    return ok


def test_glide_payload() -> bool:
    r = parse_product_extraction(NDJSON)
    stub = _StubHttp(row_ids=["ROW_A", "ROW_B", "ROW_C"]).install()
    try:
        settings = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app")
        row_ids = glide_add_product_rows(settings, "ALL_RFQ_ROW", r.products)

        ok = _check("row ids returned per product", row_ids == ["ROW_A", "ROW_B", "ROW_C"], str(row_ids))
        muts = stub.sent[0]["mutations"]
        ok &= _check(
            "targets the ALL Product table",
            muts[0]["tableName"] == "native-table-4c42a6c4-6b7c-476f-88a8-65c0e8d3c774",
        )
        first, second = muts[0]["columnValues"], muts[1]["columnValues"]
        ok &= _check("name -> Name", first["Name"] == "Hex Cap Screw M10 x 25 — 8.8")
        ok &= _check("qty -> KAbSp verbatim", first["KAbSp"] == "160,000 / 325,000 / 650,000 pcs", first["KAbSp"])
        ok &= _check("details -> K03pz", first["K03pz"] == DETAILS)
        ok &= _check("rfq id -> 3E2xY", first["3E2xY"] == "ALL_RFQ_ROW")
        ok &= _check("dwg link -> f4QCb", first["f4QCb"] == "https://example.com/dwg/iso4017.pdf")
        ok &= _check("target price -> hgVgd", second["hgVgd"] == "$2.68 - FOB India")
        ok &= _check("rep url -> LXcW2", second["LXcW2"] == "https://example.com/catalogue")
        ok &= _check("addl files -> JR0Lx keeps one uri", second["JR0Lx"] == "https://example.com/a.jpg")
        ok &= _check("accepted -> 117zS is JSON true", first["117zS"] is True)
        ok &= _check("srNo -> XbErc is a number", first["XbErc"] == 1 and second["XbErc"] == 2)
        ok &= _check("absent target price omitted", "hgVgd" not in first)
        ok &= _check("internal notes -> vizbU", first["vizbU"] == INTERNAL, first.get("vizbU", "(missing)"))
        ok &= _check("no stray columns", "" not in first)

        # An RFQ row id is mandatory: unlinked rows are orphans in a live table.
        try:
            glide_add_product_rows(settings, "   ", r.products[:1])
            ok &= _check("missing rfq id refused", False, "no error raised")
        except RuntimeError as e:
            ok &= _check("missing rfq id refused", "refusing to add unlinked" in str(e), str(e))

        # An explicitly emptied column id is left alone.
        stub.sent.clear()
        bare = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app", GLIDE_COL_PRODUCT_SR_NO="",
                        GLIDE_COL_PRODUCT_ACCEPTED="")
        glide_add_product_rows(bare, "ALL_RFQ_ROW", r.products[:1])
        cleared = stub.sent[0]["mutations"][0]["columnValues"]
        ok &= _check("emptied column ids are skipped", "XbErc" not in cleared and "117zS" not in cleared)
        return ok
    finally:
        stub.restore()


def test_query_rows_link_to_products() -> bool:
    r = parse_product_extraction(NDJSON)
    stub = _StubHttp(row_ids=["ROW_A", "ROW_B", "ROW_C"]).install()
    try:
        settings = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app")
        row_ids = glide_add_product_rows(settings, "ALL_RFQ_ROW", r.products)
        resolved = {p.index: rid for p, rid in zip(r.products, row_ids) if rid}

        stub.sent.clear()
        written = glide_add_query_rows(settings, "ALL_RFQ_ROW", r.queries, resolved)

        ok = _check("both queries written", written == 2, str(written))
        muts = stub.sent[0]["mutations"]
        ok &= _check(
            "targets the queries table",
            muts[0]["tableName"] == "native-table-19b47480-d912-462e-8721-584b5063f704",
        )
        line_q, rfq_q = muts[0]["columnValues"], muts[1]["columnValues"]
        ok &= _check("description -> Ucd5N", line_q["Ucd5N"].startswith("DIN 125 offers"))
        ok &= _check("rfq id -> Name", line_q["Name"] == "ALL_RFQ_ROW")
        # Line 2's product row came back as ROW_B, so its query must point there.
        ok &= _check("product id -> pfIJe from the returned row id", line_q["pfIJe"] == "ROW_B", str(line_q))
        ok &= _check("rfq-level query carries no product id", "pfIJe" not in rfq_q)
        ok &= _check("query id never written", "OMn91" not in line_q)
        ok &= _check("query response never written", "YoqlH" not in line_q)

        # When Glide returns no row ids, the question is still asked — against the RFQ.
        stub.sent.clear()
        glide_add_query_rows(settings, "ALL_RFQ_ROW", r.queries, {})
        degraded = stub.sent[0]["mutations"][0]["columnValues"]
        ok &= _check("unresolved product id degrades to RFQ-only", "pfIJe" not in degraded and degraded["Name"] == "ALL_RFQ_ROW")

        # Query Photo is off by default; a photo the model volunteers is not written.
        stub.sent.clear()
        q = ExtractedQuery.model_validate(
            {"description": "Which of these two revisions applies?", "product_ref": [1],
             "photo": ["https://example.com/rev-a.png"]}
        )
        glide_add_query_rows(settings, "ALL_RFQ_ROW", [q], {1: "ROW_A"})
        photo_row = stub.sent[0]["mutations"][0]["columnValues"]
        ok &= _check("photo column off by default", "KbO6i" not in photo_row, str(photo_row))

        # ...but still writeable if the column is switched on later.
        stub.sent.clear()
        with_photo = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app", GLIDE_COL_QUERY_PHOTO="KbO6i")
        glide_add_query_rows(with_photo, "ALL_RFQ_ROW", [q], {1: "ROW_A"})
        ok &= _check(
            "photo -> KbO6i when enabled",
            stub.sent[0]["mutations"][0]["columnValues"]["KbO6i"] == "https://example.com/rev-a.png",
        )
        return ok
    finally:
        stub.restore()


if __name__ == "__main__":
    passed = all(
        [
            test_parse(),
            test_validations(),
            test_banned_queries(),
            test_query_budget_and_merging(),
            test_mismatch_is_reported(),
            test_garbage_is_not_fatal(),
            test_truncation_names_the_lost_line(),
            test_glide_payload(),
            test_query_rows_link_to_products(),
        ]
    )
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    sys.exit(0 if passed else 1)
