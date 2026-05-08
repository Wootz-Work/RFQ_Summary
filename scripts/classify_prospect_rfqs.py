from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rfq_summary.config import load_settings
from rfq_summary.schema import RfqClassificationInputPayload
from rfq_summary.task import run_rfq_classification
from rfq_summary.glide_client import glide_update_prospect_rfq_classification


def cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def glide_headers(settings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.glide_api_key}",
    }


def query_prospect_rows(settings) -> list[dict[str, Any]]:
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")
    if not settings.glide_prospect_rfq_table:
        raise RuntimeError("Missing GLIDE_PROSPECT_RFQ_TABLE.")

    payload = {
        "appID": settings.glide_app_id,
        "queries": [{"tableName": settings.glide_prospect_rfq_table}],
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


def row_id_for(row: dict[str, Any]) -> str:
    for key in ("$rowID", "Row ID", "rowID", "row_id"):
        value = cell_text(row.get(key))
        if value:
            return value
    return ""


def already_classified(settings, row: dict[str, Any]) -> bool:
    cols = (
        settings.glide_col_prospect_geography,
        settings.glide_col_prospect_industry,
        settings.glide_col_prospect_client_name,
        settings.glide_col_prospect_standards,
        settings.glide_col_prospect_title,
    )
    return any(cell_text(row.get(col)) for col in cols)


def build_column_values(settings, out) -> dict[str, str]:
    values: dict[str, str] = {}
    if out.geography:
        values[settings.glide_col_prospect_geography] = out.geography
    if out.industry:
        values[settings.glide_col_prospect_industry] = out.industry
    if out.client_name:
        values[settings.glide_col_prospect_client_name] = out.client_name
    if out.standards:
        values[settings.glide_col_prospect_standards] = out.standards
    if out.title:
        values[settings.glide_col_prospect_title] = out.title
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify Prospect RFQs from Row ID + Body and write back classification columns."
    )
    parser.add_argument("--apply", action="store_true", help="Write results to Glide. Without this, only prints the plan.")
    parser.add_argument("--force", action="store_true", help="Reclassify rows even if classification columns already have values.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N eligible rows.")
    parser.add_argument("--row-id", default="", help="Process only one Prospect RFQ Row ID.")
    parser.add_argument("--include-empty-body", action="store_true", help="Include rows with empty Body.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings()
    rows = query_prospect_rows(settings)

    planned: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        row_id = row_id_for(row)
        body = cell_text(row.get("Name"))
        if args.row_id and row_id != args.row_id:
            continue
        if not row_id:
            continue
        if not body and not args.include_empty_body:
            continue
        if not args.force and already_classified(settings, row):
            continue
        planned.append((row_id, row))
        if args.limit and len(planned) >= args.limit:
            break

    print(f"Prospect RFQ rows fetched: {len(rows)}")
    print(f"Rows eligible for classification: {len(planned)}")
    print("Columns to write only: geography, industry, client name, standards, title.")
    print("Sequence is not written.")
    for row_id, row in planned:
        body_preview = cell_text(row.get("Name")).replace("\n", " ")[:120]
        print(f"  row_id={row_id} from={cell_text(row.get('vt1tN'))!r} subject={cell_text(row.get('9lbwR'))!r} body={body_preview!r}")

    if not args.apply or not planned:
        if not args.apply:
            print("Dry run only. Re-run with --apply to write to Glide.")
        return 0

    failures: list[tuple[str, str]] = []
    for index, (row_id, row) in enumerate(planned, 1):
        payload = RfqClassificationInputPayload(
            rowID=row_id,
            mail_body=cell_text(row.get("Name")),
            subject=cell_text(row.get("9lbwR")),
            from_=cell_text(row.get("vt1tN")),
            from_name=cell_text(row.get("sflMP")),
        )
        print(f"[{index}/{len(planned)}] row_id={row_id} classifying...")
        t0 = time.perf_counter()
        try:
            out = run_rfq_classification(settings, payload)
            column_values = build_column_values(settings, out)
            if column_values:
                glide_update_prospect_rfq_classification(settings, row_id, column_values)
            elapsed = int((time.perf_counter() - t0) * 1000)
            print(f"[{index}/{len(planned)}] row_id={row_id} written={column_values} elapsed_ms={elapsed}")
        except Exception as exc:
            failures.append((row_id, f"{type(exc).__name__}: {exc}"))
            print(f"[{index}/{len(planned)}] row_id={row_id} failed: {type(exc).__name__}: {exc}")

    if failures:
        print("Failures:")
        for row_id, err in failures:
            print(f"  row_id={row_id}: {err}")
        return 1

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
