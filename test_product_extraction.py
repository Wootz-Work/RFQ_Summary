"""
test_product_extraction.py
──────────────────────────
Tests the product-extraction pipeline without hitting Claude or Glide:

  1. NDJSON from the prompt parses into typed product rows
  2. lenient parsing survives fences, pretty-printed objects and junk lines
  3. rows with no name never reach the product table
  4. the Glide payload carries the right column ids, types and defaults

Run:
    python test_product_extraction.py
"""

import json
import sys

sys.path.insert(0, "src")

from rfq_summary.config import Settings
from rfq_summary.product_extraction import parse_product_extraction
from rfq_summary.glide_client import glide_add_product_rows
from rfq_summary.schema import ExtractedProduct

QUOTE_BASIS = (
    "Please quote: Unit price, MOQ, lead time, and tooling/development cost (if applicable)\n"
    "Mention the RM % cost incurred since the prices are changing."
)

DETAILS = (
    "Specification:\nMOC - SS316 / SS316L dual certified\n<br>\nScope:\n\\--\n<br>\n"
    "Application:\n\\--\n<br>\nAdditional Notes:\n" + QUOTE_BASIS
)

NDJSON = "\n".join(
    [
        json.dumps(
            {
                "type": "rfq_header",
                "customer": "Baba",
                "rfq_title": "SS fasteners package",
                "line_count_expected": 3,
                "line_count_extracted": 3,
                "reconciliation": "3 rows in the attached table, all parsed",
            }
        ),
        json.dumps(
            {
                "type": "product",
                "index": 1,
                "source_ref": "row 1",
                "name": "Spring Washer - SS316/316L",
                "structure": "single",
                "variant_count": None,
                "details": DETAILS,
                "quantity": {"value": "16", "basis": "pcs, one-time lot"},
                "target_price": None,
                "dwg_link": None,
                "rep_url": None,
                "addl_files": [],
                "annexure": None,
                "provenance": {"name": "verbatim", "application": "unknown"},
                "assumptions": [],
                "queries": [
                    {
                        "text": "Confirm end application",
                        "blocks": "line 1",
                        "field": "application",
                    }
                ],
            }
        ),
        # Family line: numeric quantity, stated target price, a drawing link,
        # and an annexure carried by reference.
        json.dumps(
            {
                "type": "product",
                "index": 2,
                "source_ref": "rows 2-15",
                "name": "Flat washers - 14 sizes, zinc plated (see annexure)",
                "structure": "family",
                "variant_count": 14,
                "details": DETAILS,
                "quantity": {"value": 20200, "basis": "pcs; also quote at supplier MOQ"},
                "target_price": "$2.68 - FOB India",
                "dwg_link": "https://example.com/dwg/washers.pdf",
                "rep_url": None,
                "addl_files": ["https://example.com/a.xlsx", "https://example.com/b.pdf"],
                "annexure": {
                    "required": True,
                    "by_reference": True,
                    "suggested_filename": "washer sizes.xlsx",
                    "columns": [],
                    "rows": [],
                },
                "provenance": {"name": "verbatim", "target_price": "verbatim"},
                "assumptions": [{"text": "Zinc plating read from the table header", "affects": "finish"}],
                "queries": [],
            }
        ),
        # Pretty-printed object spanning several lines, quantity as a bare string.
        json.dumps(
            {
                "type": "product",
                "index": 3,
                "source_ref": "email body",
                "name": "Sodium Hypochlorite Systems",
                "structure": "system",
                "details": DETAILS,
                "quantity": "4",
            },
            indent=2,
        ),
        # Scratch row: no name, must never be emitted.
        json.dumps({"type": "product", "index": 4, "name": "", "details": ""}),
        json.dumps(
            {
                "type": "rfq_summary",
                "assumptions": [],
                "queries": [{"text": "Confirm currency and incoterm", "blocks": "all", "field": "scope"}],
                "placeholder_count": 9,
                "notes_for_reviewer": "Drawings for line 3 not supplied.",
            }
        ),
    ]
)

FENCED = "Here is the output:\n```json\n" + NDJSON + "\n```\n"


def _check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail and not condition else ""))
    return condition


def test_parse() -> bool:
    ok = True
    for label, text in (("plain NDJSON", NDJSON), ("fenced + prose", FENCED)):
        result = parse_product_extraction(text)

        ok &= _check(f"[{label}] no parse errors", not result.parse_errors, str(result.parse_errors))
        ok &= _check(f"[{label}] header parsed", result.header is not None and result.header.customer == "Baba")
        ok &= _check(f"[{label}] 3 products emitted", len(result.products) == 3, f"got {len(result.products)}")
        ok &= _check(f"[{label}] unnamed row skipped", len(result.skipped_products) == 1)
        ok &= _check(f"[{label}] counts reconcile", result.reconciliation_note() == "", result.reconciliation_note())
        ok &= _check(
            f"[{label}] summary parsed",
            result.summary is not None and result.summary.placeholder_count == 9,
        )

        first, family, system = result.products
        ok &= _check(f"[{label}] order preserved", [p.index for p in result.products] == [1, 2, 3])
        ok &= _check(
            f"[{label}] qty text composed",
            first.quantity.as_qty_text() == "16 (pcs, one-time lot)",
            first.quantity.as_qty_text(),
        )
        ok &= _check(
            f"[{label}] numeric qty coerced",
            family.quantity.value == "20200",
            family.quantity.value,
        )
        ok &= _check(f"[{label}] target price kept verbatim", family.target_price == "$2.68 - FOB India")
        ok &= _check(f"[{label}] absent target price is None", first.target_price is None)
        ok &= _check(
            f"[{label}] addl files joined",
            family.addl_files_text() == "https://example.com/a.xlsx, https://example.com/b.pdf",
            family.addl_files_text(),
        )
        ok &= _check(f"[{label}] annexure by reference", bool(family.annexure and family.annexure.by_reference))
        ok &= _check(f"[{label}] bare-string qty accepted", system.quantity.as_qty_text() == "4")
        ok &= _check(f"[{label}] structure retained", [p.structure for p in result.products] == ["single", "family", "system"])
        ok &= _check(f"[{label}] line query kept", first.queries[0].blocks == "line 1")

    return ok


def test_mismatch_is_reported() -> bool:
    text = "\n".join(
        [
            json.dumps({"type": "rfq_header", "line_count_expected": 5}),
            json.dumps({"type": "product", "index": 1, "name": "M8 Lobe Pin - SS A2", "details": DETAILS}),
        ]
    )
    result = parse_product_extraction(text)
    return _check(
        "count mismatch surfaced",
        result.reconciliation_note() == "line_count_expected=5 but 1 product line(s) parsed",
        result.reconciliation_note(),
    )


def test_garbage_is_not_fatal() -> bool:
    result = parse_product_extraction("I could not find any products in this email.")
    ok = _check("prose-only output yields no products", not result.products)
    ok &= _check("prose-only output records an error", bool(result.parse_errors))
    ok &= _check("empty output yields no products", not parse_product_extraction("").products)
    return ok


def test_glide_payload() -> bool:
    """Exercise glide_add_product_rows itself against a stubbed HTTP client."""
    import httpx

    sent = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{}]

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            sent.append(json)
            return _Resp()

    real_client = httpx.Client
    httpx.Client = _Client
    try:
        # No product env vars: everything comes from the baked-in defaults.
        settings = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app")
        products = [
            ExtractedProduct.model_validate(
                {
                    "index": 1,
                    "name": "M8 Lobe Pin - SS A2",
                    "details": DETAILS,
                    "quantity": {"value": "4000", "basis": "pcs or MOQ"},
                    "target_price": "$2.68 - FOB India",
                    "dwg_link": "https://example.com/dwg.pdf",
                    "addl_files": ["https://example.com/a.xlsx", "https://example.com/b.pdf"],
                }
            ),
            ExtractedProduct.model_validate(
                {"index": 2, "name": "Spring Washer - SS316", "details": DETAILS, "quantity": {"value": "16", "basis": "pcs"}}
            ),
        ]
        written = glide_add_product_rows(settings, "ALL_RFQ_ROW", products)

        ok = _check("both rows written in one request", written == 2 and len(sent) == 1)
        mutations = sent[0]["mutations"]
        ok &= _check(
            "targets the ALL Product table",
            mutations[0]["tableName"] == "native-table-4c42a6c4-6b7c-476f-88a8-65c0e8d3c774",
        )
        first, second = mutations[0]["columnValues"], mutations[1]["columnValues"]
        ok &= _check("name -> Name", first["Name"] == "M8 Lobe Pin - SS A2")
        ok &= _check("qty -> KAbSp", first["KAbSp"] == "4000 (pcs or MOQ)", first["KAbSp"])
        ok &= _check("details -> K03pz", first["K03pz"] == DETAILS)
        ok &= _check("rfq id -> 3E2xY", first["3E2xY"] == "ALL_RFQ_ROW")
        ok &= _check("target price -> hgVgd", first["hgVgd"] == "$2.68 - FOB India")
        ok &= _check("dwg link -> f4QCb", first["f4QCb"] == "https://example.com/dwg.pdf")
        # acceptedProduct must be a JSON boolean, not the string "true".
        ok &= _check("accepted -> 117zS is JSON true", first["117zS"] is True)
        ok &= _check("srNo -> XbErc is a number", isinstance(first["XbErc"], int) and first["XbErc"] == 1)
        ok &= _check("srNo follows customer order", second["XbErc"] == 2)
        ok &= _check(
            "addl files -> JR0Lx keeps one working uri",
            first["JR0Lx"] == "https://example.com/a.xlsx",
            first["JR0Lx"],
        )
        ok &= _check("absent target price omitted", "hgVgd" not in second)
        ok &= _check("absent dwg link omitted", "f4QCb" not in second)

        # An explicitly emptied column id is left alone.
        sent.clear()
        bare = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="app", GLIDE_COL_PRODUCT_SR_NO="", GLIDE_COL_PRODUCT_ACCEPTED="")
        glide_add_product_rows(bare, "ALL_RFQ_ROW", products[:1])
        cleared = sent[0]["mutations"][0]["columnValues"]
        ok &= _check("emptied column ids are skipped", "XbErc" not in cleared and "117zS" not in cleared)
        return ok
    finally:
        httpx.Client = real_client


if __name__ == "__main__":
    passed = all(
        [
            test_parse(),
            test_mismatch_is_reported(),
            test_garbage_is_not_fatal(),
            test_glide_payload(),
        ]
    )
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    sys.exit(0 if passed else 1)
