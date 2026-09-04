from __future__ import annotations

import httpx
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from .config import Settings

if TYPE_CHECKING:
    from .schema import ExtractedProduct, ExtractedQuery


def glide_set_columns(settings: Settings, row_id: str, column_values: dict) -> None:
    if not settings.glide_api_key or not settings.glide_app_id or not settings.glide_rfq_table:
        raise RuntimeError("Missing GLIDE_* env vars (GLIDE_API_KEY/GLIDE_APP_ID/GLIDE_RFQ_TABLE).")

    url = "https://api.glideapp.io/api/function/mutateTables"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.glide_api_key}",
    }

    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "set-columns-in-row",
                "tableName": settings.glide_rfq_table,
                "rowID": row_id,
                "columnValues": column_values,
            }
        ],
    }

    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=headers, json=payload)
        r.raise_for_status()

def _glide_headers(settings: Settings) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.glide_api_key}",
    }

def glide_update_prospect_rfq_triage(settings: Settings, row_id: str, triage_text: str) -> None:
    """
    Writes triage output into Prospect RFQs table, column glide_col_prospect_triage (default: ZpJy4).
    row_id is the Row ID of the Prospect RFQ row (created by Zapier).
    """
    if not settings.enable_triage_writeback:
        return

    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")

    if not settings.glide_prospect_rfq_table:
        raise RuntimeError("Missing GLIDE_PROSPECT_RFQ_TABLE (Prospect RFQs tableName).")

    col = (settings.glide_col_prospect_triage or "").strip()
    if not col:
        raise RuntimeError("Missing GLIDE_COL_PROSPECT_TRIAGE (Prospect RFQ triage column id).")

    if not (row_id or "").strip():
        raise RuntimeError("Prospect RFQ row_id is empty; cannot write triage output.")

    url = "https://api.glideapp.io/api/function/mutateTables"
    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "set-columns-in-row",
                "tableName": settings.glide_prospect_rfq_table,
                "rowID": row_id.strip(),
                "columnValues": {col: triage_text or ""},
            }
        ],
    }

    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()


def glide_update_all_rfq_triage_outputs(
    settings: Settings,
    all_rfq_row_id: str,
    triage_text: str,
    costing_order_of_magnitude: str,
    costing_magnitude_reason: str = "",
) -> None:
    """
    Writes incoming query AI outputs to the ALL RFQ row identified by row_id.

    Writes:
      ALL RFQ.zaiResponse -> triage_text
      ALL RFQ.costingOrderOfMagnitude -> costing_order_of_magnitude
      ALL RFQ.costingMagnitudeReason -> costing_magnitude_reason
    """
    if not settings.enable_triage_writeback:
        return

    if not (all_rfq_row_id or "").strip():
        raise RuntimeError("ALL RFQ row_id is empty; cannot write triage outputs.")

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

    url = "https://api.glideapp.io/api/function/mutateTables"
    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "set-columns-in-row",
                "tableName": settings.glide_all_rfq_table,
                "rowID": all_rfq_row_id.strip(),
                "columnValues": {
                    estimate_col: costing_order_of_magnitude or "",
                    reason_col: costing_magnitude_reason or "",
                },
            }
        ],
    }

    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()


def glide_query_all_companies(settings: Settings) -> list[dict]:
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")
    if not settings.glide_all_companies_table:
        raise RuntimeError("Missing GLIDE_ALL_COMPANIES_TABLE.")

    payload = {
        "appID": settings.glide_app_id,
        "queries": [{"tableName": settings.glide_all_companies_table}],
    }

    url = "https://api.glideapp.io/api/function/queryTables"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()
        data = r.json()

    try:
        return (data or [])[0].get("rows") or []
    except Exception:
        return []


def _glide_query_table(settings: Settings, table_name: str, missing_name: str) -> list[dict]:
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")
    if not (table_name or "").strip():
        raise RuntimeError(f"Missing {missing_name}.")

    payload = {
        "appID": settings.glide_app_id,
        "queries": [{"tableName": table_name.strip()}],
    }

    url = "https://api.glideapp.io/api/function/queryTables"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()
        data = r.json()

    try:
        return (data or [])[0].get("rows") or []
    except Exception:
        return []


def glide_query_geographies(settings: Settings) -> list[dict]:
    return _glide_query_table(
        settings,
        settings.glide_geographies_table,
        "GLIDE_GEOGRAPHIES_TABLE",
    )


def glide_query_industries(settings: Settings) -> list[dict]:
    return _glide_query_table(
        settings,
        settings.glide_industries_table,
        "GLIDE_INDUSTRIES_TABLE",
    )


def glide_update_prospect_rfq_classification(
    settings: Settings,
    row_id: str,
    column_values: Dict[str, Any],
) -> None:
    if not settings.enable_triage_writeback:
        return
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")
    if not settings.glide_prospect_rfq_table:
        raise RuntimeError("Missing GLIDE_PROSPECT_RFQ_TABLE.")
    if not (row_id or "").strip():
        raise RuntimeError("Prospect RFQ row_id is empty; cannot write classification.")

    url = "https://api.glideapp.io/api/function/mutateTables"
    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "set-columns-in-row",
                "tableName": settings.glide_prospect_rfq_table,
                "rowID": row_id.strip(),
                "columnValues": column_values,
            }
        ],
    }

    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()


def glide_add_zai_regenerate_row(settings: Settings, column_values: Dict[str, Any]) -> Optional[str]:
    if not settings.enable_triage_writeback:
        return None
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")
    if not settings.glide_zai_regenerate_table:
        raise RuntimeError("Missing GLIDE_ZAI_REGENERATE_TABLE.")
    if not column_values:
        raise RuntimeError("No column values provided for ZAI Regenerate row.")

    url = "https://api.glideapp.io/api/function/mutateTables"
    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "add-row-to-table",
                "tableName": settings.glide_zai_regenerate_table,
                "columnValues": column_values,
            }
        ],
    }

    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()
        data = r.json()

    try:
        result0 = (data or [])[0] if isinstance(data, list) else data
    except Exception:
        result0 = {}
    for key in ("Row ID", "$rowID", "rowID", "row_id"):
        value = (result0 or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_row_ids(data: Any, expected: int) -> List[Optional[str]]:
    """
    Pull the Row IDs Glide returns for a batch of add-row mutations.

    The response is one entry per mutation, in order. Anything we cannot read
    comes back as None so the caller can degrade rather than mislink a row.
    """
    entries = data if isinstance(data, list) else [data]
    out: List[Optional[str]] = []
    for entry in entries[:expected]:
        row_id: Optional[str] = None
        if isinstance(entry, dict):
            for key in ("Row ID", "$rowID", "rowID", "row_id"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    row_id = value.strip()
                    break
        elif isinstance(entry, str) and entry.strip():
            row_id = entry.strip()
        out.append(row_id)
    while len(out) < expected:
        out.append(None)
    return out


def glide_add_product_rows(
    settings: Settings,
    rfq_row_id: str,
    products: list["ExtractedProduct"],
) -> List[Optional[str]]:
    """
    Adds extracted product line items to the ALL Product table, one row per line item,
    linked back to the ALL RFQ row via the rfq id column.

    Only columns configured in the environment are written, so a partially
    configured table still gets Name/Qty/Details rather than failing.

    Returns the created Row IDs, positionally aligned with `products`; an entry is
    None when Glide did not return an id for that row. Those ids are what the
    queries table's "Product id" column is filled from.
    """
    if not settings.enable_product_writeback:
        return []
    if not products:
        return []

    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")

    table = (settings.glide_all_product_table or "").strip()
    if not table:
        raise RuntimeError("Missing GLIDE_ALL_PRODUCT_TABLE.")

    name_col = (settings.glide_col_product_name or "").strip()
    if not name_col:
        raise RuntimeError("Missing GLIDE_COL_PRODUCT_NAME.")

    # Rows with no RFQ to link back to are orphans in a live table: nobody can tell
    # which enquiry they came from, and cleaning them up is manual. Refuse instead.
    if (settings.glide_col_product_rfq_id or "").strip() and not (rfq_row_id or "").strip():
        raise RuntimeError("rfq_row_id is empty; refusing to add unlinked product rows.")

    qty_col = (settings.glide_col_product_qty or "").strip()
    details_col = (settings.glide_col_product_details or "").strip()
    internal_col = (settings.glide_col_product_internal_notes or "").strip()
    rfq_id_col = (settings.glide_col_product_rfq_id or "").strip()
    target_price_col = (settings.glide_col_product_target_price or "").strip()
    dwg_link_col = (settings.glide_col_product_dwg_link or "").strip()
    rep_url_col = (settings.glide_col_product_rep_url or "").strip()
    addl_files_col = (settings.glide_col_product_addl_files or "").strip()
    sr_no_col = (settings.glide_col_product_sr_no or "").strip()
    accepted_col = (settings.glide_col_product_accepted or "").strip()

    mutations: list[Dict[str, Any]] = []
    for position, product in enumerate(products, start=1):
        column_values: Dict[str, Any] = {name_col: product.name or ""}

        if rfq_id_col and (rfq_row_id or "").strip():
            column_values[rfq_id_col] = rfq_row_id.strip()
        if qty_col:
            column_values[qty_col] = product.quantity or ""
        if details_col:
            column_values[details_col] = product.details or ""
        if internal_col and (product.internal_notes or "").strip():
            column_values[internal_col] = product.internal_notes
        if target_price_col and product.target_price:
            column_values[target_price_col] = product.target_price
        if dwg_link_col and product.dwg_link:
            # Also a single uri. The model sometimes comma-joins several documents,
            # which renders as one broken link rather than any working one.
            links = [u.strip() for u in str(product.dwg_link).split(",") if u.strip()]
            column_values[dwg_link_col] = links[0] if links else product.dwg_link
            if len(links) > 1:
                print(
                    f"[WARN] product {product.name!r}: {len(links) - 1} additional drawing link(s) "
                    f"dropped — 'Dwg link' holds one uri"
                )
        if rep_url_col and product.rep_url:
            column_values[rep_url_col] = product.rep_url
        if addl_files_col:
            # The Glide column is a single uri, so only the first file fits; joining
            # them would produce a string that is not a working link.
            files = [u for u in (product.addl_files or []) if (u or "").strip()]
            if files:
                column_values[addl_files_col] = files[0]
                if len(files) > 1:
                    print(
                        f"[WARN] product {product.name!r}: {len(files) - 1} additional file link(s) "
                        f"dropped — 'Addl. files' holds one uri"
                    )
        if sr_no_col:
            column_values[sr_no_col] = product.index if product.index is not None else position
        if accepted_col:
            column_values[accepted_col] = True

        mutations.append(
            {
                "kind": "add-row-to-table",
                "tableName": table,
                "columnValues": column_values,
            }
        )

    url = "https://api.glideapp.io/api/function/mutateTables"
    batch_size = max(1, int(settings.glide_product_rows_per_request))
    row_ids: List[Optional[str]] = []

    with httpx.Client(timeout=120) as client:
        for start in range(0, len(mutations), batch_size):
            batch = mutations[start : start + batch_size]
            r = client.post(
                url,
                headers=_glide_headers(settings),
                json={"appID": settings.glide_app_id, "mutations": batch},
            )
            r.raise_for_status()
            try:
                row_ids.extend(_extract_row_ids(r.json(), len(batch)))
            except Exception:
                row_ids.extend([None] * len(batch))

    return row_ids


def glide_add_query_rows(
    settings: Settings,
    rfq_row_id: str,
    queries: list["ExtractedQuery"],
    product_row_ids: Dict[int, str],
) -> int:
    """
    Adds one row per open question to the queries table.

    `product_row_ids` maps a product's index to the Row ID it was written as, so
    each query can point at the line it blocks. A query whose product row id is
    unknown, and every RFQ-level query, is still written — linked to the RFQ but
    with no product — because losing the question entirely is worse.

    Query ID is database-assigned and Query Response belongs to the customer;
    neither is written here.

    Returns the number of rows written.
    """
    if not settings.enable_query_writeback:
        return 0
    if not queries:
        return 0

    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")

    table = (settings.glide_queries_table or "").strip()
    if not table:
        raise RuntimeError("Missing GLIDE_QUERIES_TABLE.")

    description_col = (settings.glide_col_query_description or "").strip()
    if not description_col:
        raise RuntimeError("Missing GLIDE_COL_QUERY_DESCRIPTION.")

    rfq_id_col = (settings.glide_col_query_rfq_id or "").strip()
    if rfq_id_col and not (rfq_row_id or "").strip():
        raise RuntimeError("rfq_row_id is empty; refusing to add unlinked query rows.")

    product_id_col = (settings.glide_col_query_product_id or "").strip()
    photo_col = (settings.glide_col_query_photo or "").strip()

    mutations: list[Dict[str, Any]] = []
    for query in queries:
        column_values: Dict[str, Any] = {description_col: query.description or ""}

        if rfq_id_col and (rfq_row_id or "").strip():
            column_values[rfq_id_col] = rfq_row_id.strip()

        if product_id_col and query.product_refs:
            # One question covering several lines is one row carrying all their ids.
            resolved = [product_row_ids[ref] for ref in query.product_refs if ref in product_row_ids]
            if resolved:
                column_values[product_id_col] = ", ".join(resolved)
            unresolved = [ref for ref in query.product_refs if ref not in product_row_ids]
            if unresolved:
                print(
                    f"[WARN] query {query.query_ref or '?'} points at line(s) {unresolved}, "
                    f"whose row ids are unknown — linked to the rest only"
                )

        if photo_col and query.photo_text():
            column_values[photo_col] = query.photo_text()

        mutations.append(
            {
                "kind": "add-row-to-table",
                "tableName": table,
                "columnValues": column_values,
            }
        )

    url = "https://api.glideapp.io/api/function/mutateTables"
    batch_size = max(1, int(settings.glide_product_rows_per_request))
    written = 0

    with httpx.Client(timeout=120) as client:
        for start in range(0, len(mutations), batch_size):
            batch = mutations[start : start + batch_size]
            r = client.post(
                url,
                headers=_glide_headers(settings),
                json={"appID": settings.glide_app_id, "mutations": batch},
            )
            r.raise_for_status()
            written += len(batch)

    return written


def _glide_query_rowid_by_rfq_id(settings: Settings, rfq_id: str) -> Optional[str]:
    """
    Uses Glide Advanced API (queryTables) with SQL to find the ZAI Responses row where:
      <rfqIdColumn> = rfq_id
    Returns the Row ID of the matching row, or None.
    """
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")

    if not settings.glide_zai_responses_table:
        raise RuntimeError("Missing GLIDE_ZAI_RESPONSES_TABLE (target table for writeback).")

    col = (settings.glide_col_rfq_id or "").strip()
    if not col:
        raise RuntimeError("Missing GLIDE_COL_RFQ_ID (rfqId column id in ZAI Responses table).")

    # Glide docs: queryTables supports SQL with params.  [oai_citation:1‡Glide](https://www.glideapps.com/docs/using-glide-tables-api)
    sql = f'SELECT * FROM "{settings.glide_zai_responses_table}" WHERE "{col}" = $1 LIMIT 1'
    payload = {
        "appID": settings.glide_app_id,
        "queries": [{"sql": sql, "params": [rfq_id]}],
    }

    url = "https://api.glideapp.io/api/function/queryTables"
    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()
        data = r.json()

    # Response is an array with one element per query: { rows: [...], next: ... }  [oai_citation:2‡Glide](https://www.glideapps.com/docs/using-glide-tables-api)
    try:
        rows = (data or [])[0].get("rows") or []
    except Exception:
        rows = []

    if not rows:
        return None

    row0 = rows[0] if isinstance(rows[0], dict) else {}
    # Row ID key is typically "Row ID" for native tables, but handle variants defensively.
    for k in ("Row ID", "rowID", "row_id"):
        v = row0.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return None


def _glide_add_row_with_rfq_id(settings: Settings, rfq_id: str) -> str:
    """
    Adds a row to ZAI Responses table, setting rfqId column to rfq_id.
    Returns the created Row ID (if available), else raises.
    """
    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")

    if not settings.glide_zai_responses_table:
        raise RuntimeError("Missing GLIDE_ZAI_RESPONSES_TABLE.")

    col = (settings.glide_col_rfq_id or "").strip()
    if not col:
        raise RuntimeError("Missing GLIDE_COL_RFQ_ID.")

    url = "https://api.glideapp.io/api/function/mutateTables"
    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "add-row-to-table",
                "tableName": settings.glide_zai_responses_table,
                "columnValues": {col: rfq_id},
            }
        ],
    }

    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()
        data = r.json()

    # Per docs, result can include "Row ID" of added row.  [oai_citation:3‡Glide](https://www.glideapps.com/docs/using-glide-tables-api)
    try:
        result0 = (data or [])[0] if isinstance(data, list) else data
    except Exception:
        result0 = {}

    for k in ("Row ID", "rowID", "row_id"):
        v = (result0 or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    raise RuntimeError("Glide add-row-to-table succeeded but Row ID not returned.")


def glide_upsert_zai_response_by_rfq_id(settings: Settings, rfq_id: str, column_values: Dict[str, Any]) -> None:
    """
    Upsert behavior:
      1) queryTables to find ZAI Responses row where rfqId == rfq_id
      2) if found -> set-columns-in-row on that row
      3) if not found -> add-row-to-table with rfqId set -> set-columns-in-row
    """
    if not rfq_id.strip():
        raise RuntimeError("rfq_id is empty; cannot upsert into ZAI Responses table.")

    # Find existing ZAI Responses rowID
    row_id = _glide_query_rowid_by_rfq_id(settings, rfq_id.strip())

    # If missing, create new row with rfqId populated
    if not row_id:
        row_id = _glide_add_row_with_rfq_id(settings, rfq_id.strip())

    # Now write columns into ZAI Responses row
    url = "https://api.glideapp.io/api/function/mutateTables"
    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "set-columns-in-row",
                "tableName": settings.glide_zai_responses_table,
                "rowID": row_id,
                "columnValues": column_values,
            }
        ],
    }

    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_glide_headers(settings), json=payload)
        r.raise_for_status()
