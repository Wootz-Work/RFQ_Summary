from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rfq_summary.attachments import analyze_attachments
from rfq_summary.config import load_settings
from rfq_summary.llm import generate_text, load_prompt_file
from rfq_summary.schema import QueryPayload
from rfq_summary.task import _build_query_triage_prompt, _join_attachment_text_any, _unwrap_tagged_output


TARGET_ROW_IDS = [
    "08FKwsCrTd2pG.nFRT.o1w",
    "a-79y91LvSsaYDNR-Z3Pifg",
]

URL_RE = re.compile(r"https?://[^\s,<>\"]+")

ALL_RFQ_COLUMNS = {
    "title": "QdiyR",
    "deadline": "r8pRq",
    "industry": "iUuzA",
    "geography": "2K6Rk",
    "standard": "7SDcI",
    "customerName": "BietT",
    "rfqSequence": "TsFhb",
    "quotationFolderLink": "Q5TZ2",
    "currentStatus": "6P9Ok",
    "team": "GAQgI",
    "requiredBy": "h3POw",
    "receivedDate": "xbe3m",
    "rfqCreatedDate": "3Wgie",
    "createdBy": "ioVbg",
    "salesPor": "DVjHx",
    "rfqPoCInternalPoC": "epQFl",
    "prospectRfqProspectRowId": "JZO7K",
    "zaiResponse": "MANCF",
    "jsonUniversalSearchText": "NOd9j",
    "jsonCompanySearchText": "4FmsZ",
    "jsonStatusSearch": "r2Qk7",
    "specialInstructions2": "TSSyP",
}


def cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def glide_headers(settings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.glide_api_key}",
    }


def row_id_for(row: dict[str, Any]) -> str:
    for key in ("$rowID", "Row ID", "rowID", "row_id"):
        value = cell_text(row.get(key))
        if value:
            return value
    return ""


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        value = cell_text(value).rstrip(").,]")
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def extract_urls(*values: object) -> list[str]:
    urls: list[str] = []
    for value in values:
        urls.extend(URL_RE.findall(cell_text(value)))
    return dedupe(urls)


def query_all_rfq_rows(settings) -> list[dict[str, Any]]:
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")
    if not settings.glide_all_rfq_table:
        raise RuntimeError("Missing GLIDE_ALL_RFQ_TABLE.")

    payload = {
        "appID": settings.glide_app_id,
        "queries": [{"tableName": settings.glide_all_rfq_table}],
    }
    with httpx.Client(timeout=60) as client:
        response = client.post(
            "https://api.glideapp.io/api/function/queryTables",
            headers=glide_headers(settings),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    try:
        rows = (data or [])[0].get("rows") or []
    except Exception:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def write_costing_value(settings, row_id: str, estimate: str, reason: str) -> None:
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")
    if not settings.glide_all_rfq_table:
        raise RuntimeError("Missing GLIDE_ALL_RFQ_TABLE.")

    estimate_col = (settings.glide_col_all_rfq_costing_order_of_magnitude or "").strip()
    reason_col = (settings.glide_col_all_rfq_costing_magnitude_reason or "").strip()
    if not estimate_col:
        raise RuntimeError("Missing GLIDE_COL_ALL_RFQ_COSTING_ORDER_OF_MAGNITUDE.")
    if not reason_col:
        raise RuntimeError("Missing GLIDE_COL_ALL_RFQ_COSTING_MAGNITUDE_REASON.")

    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "set-columns-in-row",
                "tableName": settings.glide_all_rfq_table,
                "rowID": row_id.strip(),
                "columnValues": {
                    estimate_col: estimate or "",
                    reason_col: reason or "",
                },
            }
        ],
    }
    with httpx.Client(timeout=60) as client:
        response = client.post(
            "https://api.glideapp.io/api/function/mutateTables",
            headers=glide_headers(settings),
            json=payload,
        )
        response.raise_for_status()


def build_body(row: dict[str, Any]) -> str:
    lines: list[str] = []
    for label, col_id in ALL_RFQ_COLUMNS.items():
        value = cell_text(row.get(col_id))
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def build_payload(row_id: str, row: dict[str, Any]) -> QueryPayload:
    attachment_urls = extract_urls(row.get(ALL_RFQ_COLUMNS["quotationFolderLink"]))
    return QueryPayload(
        row_id=row_id,
        subject=cell_text(row.get(ALL_RFQ_COLUMNS["title"])),
        from_="",
        from_name=cell_text(row.get(ALL_RFQ_COLUMNS["customerName"])),
        body=build_body(row),
        received_at=cell_text(row.get(ALL_RFQ_COLUMNS["receivedDate"])),
        attachment_urls=attachment_urls,
        attached_media=[],
    )


def generate_costing(settings, payload: QueryPayload) -> tuple[str, str, str, int]:
    t_attach0 = time.perf_counter()
    attachment_findings = analyze_attachments(settings, payload.all_attachment_urls())
    extracted_text = _join_attachment_text_any("", attachment_findings)
    attachments_ms = int((time.perf_counter() - t_attach0) * 1000)

    prompt_template = load_prompt_file(settings.prompt_query_costing_file)
    user_prompt = _build_query_triage_prompt(prompt_template, payload, extracted_text)
    raw = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=user_prompt,
    )
    estimate = _unwrap_tagged_output(raw, "estimate")
    reason = _unwrap_tagged_output(raw, "reason")
    if not reason:
        reason = (
            "Bracket selected from available quantity, part, material, and process signals."
            if estimate
            else "Insufficient product description or quantity to estimate confidently."
        )
    return estimate, reason, raw, attachments_ms


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate costing estimates for ALL RFQ row IDs and write only AEa95 + 1UY5w."
    )
    parser.add_argument("--apply", action="store_true", help="Write regenerated estimates and reasons to Glide.")
    parser.add_argument("--row-id", action="append", default=[], help="ALL RFQ Row ID to process. Repeatable.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_ids = args.row_id or TARGET_ROW_IDS

    settings = load_settings()
    rows = query_all_rfq_rows(settings)
    by_row_id = {row_id_for(row): row for row in rows if row_id_for(row)}

    missing = [row_id for row_id in target_ids if row_id not in by_row_id]
    if missing:
        print("Missing row IDs in Glide query:")
        for row_id in missing:
            print(f"  {row_id}")

    planned = [(row_id, by_row_id[row_id]) for row_id in target_ids if row_id in by_row_id]
    print(f"Rows requested: {len(target_ids)}")
    print(f"Rows found: {len(planned)}")
    print(f"Apply writeback: {bool(args.apply)}")

    failures: list[tuple[str, str]] = []
    for index, (row_id, row) in enumerate(planned, 1):
        payload = build_payload(row_id, row)
        existing_estimate = cell_text(row.get(settings.glide_col_all_rfq_costing_order_of_magnitude))
        existing_reason = cell_text(row.get(settings.glide_col_all_rfq_costing_magnitude_reason))
        print(
            f"[{index}/{len(planned)}] row_id={row_id} title={payload.subject!r} "
            f"existing_estimate={existing_estimate!r} existing_reason={existing_reason!r}"
        )
        t0 = time.perf_counter()
        try:
            estimate, reason, raw, attachments_ms = generate_costing(settings, payload)
            elapsed = int((time.perf_counter() - t0) * 1000)
            print(f"[{index}/{len(planned)}] estimate={estimate!r} reason={reason!r}")
            print(f"[{index}/{len(planned)}] attachments_ms={attachments_ms} total_ms={elapsed}")
            if not estimate:
                print(f"[{index}/{len(planned)}] raw output={raw!r}")
            if args.apply:
                write_costing_value(settings, row_id, estimate, reason)
                print(f"[{index}/{len(planned)}] wrote AEa95={estimate!r} 1UY5w={reason!r}")
        except Exception as exc:
            failures.append((row_id, f"{type(exc).__name__}: {exc}"))
            print(f"[{index}/{len(planned)}] failed: {type(exc).__name__}: {exc}")

    if failures:
        print("Failures:")
        for row_id, err in failures:
            print(f"  {row_id}: {err}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
