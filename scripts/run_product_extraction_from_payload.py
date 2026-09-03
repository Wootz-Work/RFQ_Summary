"""
Dry run of the product extraction that /query/triage performs, without touching
Glide. Parses the attachments, runs the product prompt, prints the line items and
the exact Glide mutations that would be sent.

Usage:
    venv/bin/python scripts/run_product_extraction_from_payload.py payload_query.json
    venv/bin/python scripts/run_product_extraction_from_payload.py payload_query.json --ndjson
    venv/bin/python scripts/run_product_extraction_from_payload.py payload_query.json --apply

The payload is the same body /query/triage accepts, e.g.:

    {
      "rowID": "ALL_RFQ_ROW_ID",
      "subject": "RFQ - SS lobe pins",
      "from_name": "Baba",
      "body": "Please quote the attached list ...",
      "attachment_urls": ["https://.../RFQ.xlsx"]
    }

Without --apply nothing is written anywhere: the run is read-only against Drive
and Claude. With --apply the rows go to GLIDE_ALL_PRODUCT_TABLE, so point that at
a scratch table before using it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rfq_summary.attachments import analyze_attachments
from rfq_summary.config import load_settings
from rfq_summary.glide_client import glide_add_product_rows
from rfq_summary.llm import load_prompt_file
from rfq_summary.product_extraction import parse_product_extraction
from rfq_summary.schema import QueryPayload
from rfq_summary.task import (
    _build_query_triage_prompt,
    _generate_text_with_timing,
    _join_attachment_text_any,
)


def _preview(text: str, width: int = 70) -> str:
    return (text or "").replace("\n", " ⏎ ")[:width]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", help="Path to a /query/triage payload JSON file")
    parser.add_argument("--ndjson", action="store_true", help="Print the raw NDJSON the model returned")
    parser.add_argument("--details", action="store_true", help="Print the full Details markdown per line")
    parser.add_argument("--apply", action="store_true", help="Actually write the rows to Glide")
    args = parser.parse_args()

    settings = load_settings()
    payload = QueryPayload.model_validate(json.loads(Path(args.payload).read_text(encoding="utf-8")))

    print(f"Attachments: {payload.all_attachment_urls()}")
    findings = analyze_attachments(settings, payload.all_attachment_urls())
    extracted_text = _join_attachment_text_any("", findings)
    print(f"Parsed {len(findings)} attachment(s), {len(extracted_text)} chars of extracted text\n")

    template = load_prompt_file(settings.prompt_product_extraction_file)
    prompt = _build_query_triage_prompt(template, payload, extracted_text)
    model_text, llm_ms = _generate_text_with_timing(settings, prompt)
    print(f"Product prompt returned in {llm_ms} ms\n")

    if args.ndjson:
        print("--- raw NDJSON ---")
        print(model_text)
        print("--- end ---\n")

    result = parse_product_extraction(model_text)

    if result.header:
        print(f"Customer            : {result.header.customer}")
        print(f"RFQ title           : {result.header.rfq_title}")
        print(f"Lines expected      : {result.header.line_count_expected}")
        print(f"Lines extracted     : {len(result.products)}")
        print(f"Reconciliation      : {result.header.reconciliation}")
    if result.reconciliation_note():
        print(f"!! COUNT MISMATCH   : {result.reconciliation_note()}")
    if result.skipped_products:
        print(f"!! Rows skipped     : {len(result.skipped_products)} (no name)")
    if result.parse_errors:
        print(f"!! Parse errors     : {result.parse_errors}")

    print(f"\n{'#':<3} {'structure':<9} {'qty':<28} name")
    print("-" * 110)
    for i, product in enumerate(result.products, start=1):
        print(f"{i:<3} {product.structure:<9} {_preview(product.quantity.as_qty_text(), 27):<28} {product.name}")
        if product.target_price:
            print(f"     target price: {product.target_price}")
        if product.dwg_link:
            print(f"     dwg link    : {product.dwg_link}")
        if product.queries:
            for q in product.queries:
                print(f"     query       : {q.text} (blocks {q.blocks or '?'})")
        if args.details:
            print("     ---")
            for line in (product.details or "").splitlines():
                print(f"     {line}")
            print("     ---")

    if result.summary:
        print(f"\nPlaceholders        : {result.summary.placeholder_count}")
        print(f"Notes for reviewer  : {result.summary.notes_for_reviewer}")
        for q in result.summary.queries:
            print(f"All-lines query     : {q.text} (blocks {q.blocks or '?'})")

    print(f"\nWould write to table: {settings.glide_all_product_table or '<unset>'}")
    for i, product in enumerate(result.products, start=1):
        column_values = {settings.glide_col_product_name: product.name}
        if settings.glide_col_product_rfq_id:
            column_values[settings.glide_col_product_rfq_id] = payload.row_id
        if settings.glide_col_product_qty:
            column_values[settings.glide_col_product_qty] = product.quantity.as_qty_text()
        if settings.glide_col_product_details:
            column_values[settings.glide_col_product_details] = _preview(product.details, 60) + " ..."
        print(f"  row {i}: {json.dumps(column_values, ensure_ascii=False)}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to write to Glide.")
        return 0

    written = glide_add_product_rows(settings, payload.row_id, result.products)
    print(f"\nWrote {written} row(s) to {settings.glide_all_product_table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
