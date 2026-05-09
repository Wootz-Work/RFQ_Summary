from __future__ import annotations

import httpx
from typing import Any, Dict, Optional
from .config import Settings


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
) -> None:
    """
    Writes incoming query AI outputs to the ALL RFQ row identified by row_id.

    Writes:
      ALL RFQ.zaiResponse -> triage_text
      ALL RFQ.costingOrderOfMagnitude -> costing_order_of_magnitude
    """
    if not settings.enable_triage_writeback:
        return

    if not (all_rfq_row_id or "").strip():
        raise RuntimeError("ALL RFQ row_id is empty; cannot write triage outputs.")

    if not settings.glide_api_key or not settings.glide_app_id:
        raise RuntimeError("Missing GLIDE_API_KEY/GLIDE_APP_ID.")

    if not settings.glide_all_rfq_table:
        raise RuntimeError("Missing GLIDE_ALL_RFQ_TABLE.")

    zai_col = (settings.glide_col_all_rfq_zai_response or "").strip()
    estimate_col = (settings.glide_col_all_rfq_costing_order_of_magnitude or "").strip()
    if not zai_col:
        raise RuntimeError("Missing GLIDE_COL_ALL_RFQ_ZAI_RESPONSE.")
    if not estimate_col:
        raise RuntimeError("Missing GLIDE_COL_ALL_RFQ_COSTING_ORDER_OF_MAGNITUDE.")

    url = "https://api.glideapp.io/api/function/mutateTables"
    payload = {
        "appID": settings.glide_app_id,
        "mutations": [
            {
                "kind": "set-columns-in-row",
                "tableName": settings.glide_all_rfq_table,
                "rowID": all_rfq_row_id.strip(),
                "columnValues": {
                    zai_col: triage_text or "",
                    estimate_col: costing_order_of_magnitude or "",
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
