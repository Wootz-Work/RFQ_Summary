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
                "product_ref": 2,
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
            json.dumps({"type": "query", "query_ref": "Q1", "product_ref": 1, "section": "scope",
                        "description": "Confirm the scope boundary."}),
            json.dumps({"type": "query", "query_ref": "Q2", "product_ref": 1, "section": "scope",
                        "description": "confirm the scope boundary."}),
            json.dumps({"type": "query", "query_ref": "Q3", "product_ref": 9, "section": "quantity",
                        "description": "Is this annual? And what incoterm applies?"}),
            json.dumps({"type": "query", "query_ref": "Q4", "product_ref": 1, "section": "pricing",
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
    ok &= _check("query pointing at a missing line flagged", "was not extracted" in w, w)

    # A response is the customer's to give; never accept one from the model.
    q = ExtractedQuery.model_validate({"description": "x?", "Query Response": "B1", "response": "B1"})
    ok &= _check("model-supplied response discarded", not hasattr(q, "response"))
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
            {"description": "Which of these two revisions applies?", "product_ref": 1,
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
            test_mismatch_is_reported(),
            test_garbage_is_not_fatal(),
            test_glide_payload(),
            test_query_rows_link_to_products(),
        ]
    )
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    sys.exit(0 if passed else 1)
