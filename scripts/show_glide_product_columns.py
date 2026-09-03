"""
Read-only: print the column ids of the ALL Product table so they can be copied
into .env as GLIDE_COL_PRODUCT_*. Writes nothing.

Usage:
    venv/bin/python scripts/show_glide_product_columns.py
    venv/bin/python scripts/show_glide_product_columns.py --table native-table-xxxx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rfq_summary.config import load_settings
from rfq_summary.glide_client import _glide_query_table

# Column ids Glide adds to every row; not settable by us.
SYSTEM_KEYS = {"$rowID", "Row ID", "rowID", "row_id"}

# Best-guess mapping from the display names in the ALL Product export.
ENV_HINTS = {
    "name": "GLIDE_COL_PRODUCT_NAME",
    "qty": "GLIDE_COL_PRODUCT_QTY",
    "details": "GLIDE_COL_PRODUCT_DETAILS",
    "rfq id": "GLIDE_COL_PRODUCT_RFQ_ID",
    "rfqid": "GLIDE_COL_PRODUCT_RFQ_ID",
    "target price": "GLIDE_COL_PRODUCT_TARGET_PRICE",
    "dwg link": "GLIDE_COL_PRODUCT_DWG_LINK",
    "rep url": "GLIDE_COL_PRODUCT_REP_URL",
    "addl. files": "GLIDE_COL_PRODUCT_ADDL_FILES",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--table",
        default="",
        help="Glide tableName to inspect (defaults to GLIDE_ALL_PRODUCT_TABLE)",
    )
    parser.add_argument("--rows", type=int, default=3, help="Sample rows to show per column")
    args = parser.parse_args()

    settings = load_settings()
    table = (args.table or settings.glide_all_product_table or "").strip()
    if not table:
        print("No table given. Pass --table, or set GLIDE_ALL_PRODUCT_TABLE in .env.")
        return 1

    rows = _glide_query_table(settings, table, "GLIDE_ALL_PRODUCT_TABLE")
    if not rows:
        print(f"Table {table!r} returned no rows — add one row in Glide so the columns are visible.")
        return 1

    # Rows only carry the columns that have values, so union across the sample.
    keys: list[str] = []
    for row in rows[: max(args.rows, 20)]:
        for k in row.keys():
            if k not in keys and k not in SYSTEM_KEYS:
                keys.append(k)

    print(f"Table: {table}")
    print(f"Rows returned: {len(rows)}\n")
    print(f"{'column id / name':<40} sample values")
    print("-" * 100)
    for key in keys:
        samples = []
        for row in rows:
            val = row.get(key)
            if val not in (None, ""):
                samples.append(str(val).replace("\n", " ")[:40])
            if len(samples) >= args.rows:
                break
        print(f"{key:<40} {' | '.join(samples)}")

    print("\nSuggested .env lines (verify each against the samples above):")
    print(f"GLIDE_ALL_PRODUCT_TABLE={table}")
    for key in keys:
        env_name = ENV_HINTS.get(key.strip().lower())
        if env_name:
            print(f"{env_name}={key}")
    print(
        "\nColumn ids that are not obviously named (e.g. 'qty', 'det') have to be matched\n"
        "by their sample values — the id is what Glide returns, not the display name."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
