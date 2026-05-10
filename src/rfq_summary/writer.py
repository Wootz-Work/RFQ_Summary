from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict

from .config import Settings
from .schema import InputPayload, OutputPayload, QueryPayload, TriageOutputPayload, RfqClassificationInputPayload, RfqClassificationOutputPayload, RfqRegenerateTriageInputPayload, RfqRegenerateTriageOutputPayload, RfqQueryInputPayload, RfqQueryOutputPayload
from .glide_client import glide_upsert_zai_response_by_rfq_id, glide_update_all_rfq_triage_outputs, glide_update_prospect_rfq_classification, glide_add_zai_regenerate_row
from .gsheet_logger import append_rows, build_chunked_log_rows


def _print_terminal(out: OutputPayload) -> None:
    print("\n==============================")
    print(f"MODE: {out.mode}  RUN_ID: {out.run_id}  ROW_ID: {out.row_id}")
    print("==============================\n")

    if out.mode == "pricing":
        print("=== OUTPUT 1 (Pricing Estimate) ===\n")
        print(out.pricing_estimate_text or "")
        print("\n=== OUTPUT 2 (Reasoning) ===\n")
        print(out.pricing_reasoning_text or "")
    elif out.mode == "summary":
        print("=== SUMMARY ===\n")
        print(out.summary_text or "")
        print("\n=== SCOPE ===\n")
        print(out.scope_text or "")
        print("\n=== COST ===\n")
        print(out.cost_text or "")
        print("\n=== QUALITY ===\n")
        print(out.quality_text or "")
        print("\n=== TIMELINE ===\n")
        print(out.timeline_text or "")
    elif out.mode == "all":
        print("=== OUTPUT 1 (Pricing Estimate) ===\n")
        print(out.pricing_estimate_text or "")
        print("\n=== OUTPUT 2 (Reasoning) ===\n")
        print(out.pricing_reasoning_text or "")
        print("\n=== OUTPUT 3 (RFQ Summary) ===\n")
        print(out.summary_text or "")
    else:
        print("=== OUTPUT ===\n")
        print(out.raw_model_output or "")

    print("\n==============================\n")


def write_all(settings: Settings, inp: InputPayload, out: OutputPayload) -> None:
    """
    - If ENABLE_GLIDE_WRITEBACK=true: write to Glide first, then log to Sheets.
    - If ENABLE_GLIDE_WRITEBACK=false: print to terminal (safe), then log to Sheets.
    - Logging is chunked so we don't lose any data.
    """

    colvals: Dict[str, str] = {}

    if out.mode == "pricing":
        # OUTPUT 1 -> pricingEstimate
        colvals[settings.glide_col_pricing_estimate] = out.pricing_estimate_text or ""
        # OUTPUT 2 -> pricingEstimateSummary
        colvals[settings.glide_col_pricing_estimate_summary] = out.pricing_reasoning_text or ""

    elif out.mode == "summary":
        # Summary prompt is split into 5 cards (Summary + 4 detailed cards)
        colvals[settings.glide_col_summary] = out.summary_text or ""
        colvals[settings.glide_col_scope] = out.scope_text or ""
        colvals[settings.glide_col_cost] = out.cost_text or ""
        colvals[settings.glide_col_quality] = out.quality_text or ""
        colvals[settings.glide_col_schedule] = out.timeline_text or ""

    elif out.mode == "all":
        # /rfq/run writes both pricing outputs + all summary cards
        colvals[settings.glide_col_pricing_estimate] = out.pricing_estimate_text or ""
        colvals[settings.glide_col_pricing_estimate_summary] = out.pricing_reasoning_text or ""

        colvals[settings.glide_col_summary] = out.summary_text or ""
        colvals[settings.glide_col_scope] = out.scope_text or ""
        colvals[settings.glide_col_cost] = out.cost_text or ""
        colvals[settings.glide_col_quality] = out.quality_text or ""
        colvals[settings.glide_col_schedule] = out.timeline_text or ""

    else:
        raise RuntimeError(f"Unknown mode: {out.mode}")

    # 1) Writeback OR terminal print
    if settings.enable_glide_writeback:
        if not out.row_id.strip():
            raise RuntimeError("row_id missing but ENABLE_GLIDE_WRITEBACK=true")

        # out.row_id is the RowID of the RFQ in "ALL RFQ" table.
        # We store that into ZAI Responses.rfqId (usIzP) and upsert outputs into that row.
        glide_upsert_zai_response_by_rfq_id(settings, out.row_id, colvals)
    else:
        _print_terminal(out)

    # 2) Build log fields (store everything; chunked rows preserve all)
    input_json = json.dumps(
        {
            "rowID": inp.row_id,
            "Title": inp.title,
            "Industry": inp.industry,
            "Geography": inp.geography,
            "Standard": inp.standard,
            "Customer name": inp.customer_name,
            "Product_json": inp.product_json,
        },
        ensure_ascii=False,
    )

    extracted_text = inp.extracted_attachment_text or ""

    web_text = ""
    if out.web_findings:
        web_text = "\n\n".join([f"{w.title} {w.url}\n{w.snippet}".strip() for w in out.web_findings])

    fields = {
        "input_json": input_json,
        "extracted_attachment_text": extracted_text,
        "pricing_estimate_text": out.pricing_estimate_text or "",
        "pricing_reasoning_text": out.pricing_reasoning_text or "",
        "summary_text": out.summary_text or "",
        "scope_text": out.scope_text or "",
        "cost_text": out.cost_text or "",
        "quality_text": out.quality_text or "",
        "timeline_text": out.timeline_text or "",
        "raw_model_output": out.raw_model_output or "",
        "web_findings": web_text,
        "timings": json.dumps(out.timings or {}, ensure_ascii=False),
        "docai": json.dumps(out.docai or {}, ensure_ascii=False),
        "glide_column_values": json.dumps(colvals, ensure_ascii=False),
        "writeback_enabled": str(bool(settings.enable_glide_writeback)),
    }

    rows = build_chunked_log_rows(
        settings=settings,
        run_id=out.run_id,
        mode=out.mode,
        row_id=out.row_id,
        fields=fields,
    )
    append_rows(settings, rows)


def write_triage(settings: Settings, inp: QueryPayload, out: TriageOutputPayload) -> None:
    """
    Writes triage outputs into the linked ALL RFQ row.
    Also logs to Sheets (chunked).
    """
    if settings.enable_triage_writeback:
        glide_update_all_rfq_triage_outputs(
            settings,
            out.row_id,
            out.triage_text or "",
            out.costing_estimate_text or "",
        )

    # Log to Sheets (same sheet schema; different field names)
    fields = {
        "subject": inp.subject,
        "from": inp.from_,
        "from_name": inp.from_name,
        "body": inp.body,
        "received_at": inp.received_at,
        "attachment_urls": json.dumps(inp.attachment_urls or [], ensure_ascii=False),  # replaces query_json
        "attached_media": json.dumps(inp.attached_media or [], ensure_ascii=False),
        "triage_text": out.triage_text or "",
        "costing_estimate_text": out.costing_estimate_text or "",
        "raw_model_output": out.raw_model_output or "",
        "raw_costing_model_output": out.raw_costing_model_output or "",
        "timings": json.dumps(out.timings or {}, ensure_ascii=False),
        "docai": json.dumps(out.docai or {}, ensure_ascii=False),
        "writeback_enabled": str(bool(settings.enable_triage_writeback)),
    }

    rows = build_chunked_log_rows(
        settings=settings,
        run_id=out.run_id,
        mode="triage",
        row_id=out.row_id,
        fields=fields,
    )
    append_rows(settings, rows)


def write_rfq_classification(
    settings: Settings,
    inp: RfqClassificationInputPayload,
    out: RfqClassificationOutputPayload,
) -> None:
    if settings.enable_triage_writeback:
        column_values: Dict[str, str] = {}
        if out.geography:
            column_values[settings.glide_col_prospect_geography] = out.geography
        if out.industry:
            column_values[settings.glide_col_prospect_industry] = out.industry
        if out.client_name:
            column_values[settings.glide_col_prospect_client_name] = out.client_name
        if out.standards:
            column_values[settings.glide_col_prospect_standards] = out.standards
        if out.title:
            column_values[settings.glide_col_prospect_title] = out.title
        if out.sequence:
            column_values[settings.glide_col_prospect_sequence] = out.sequence

        if column_values:
            glide_update_prospect_rfq_classification(settings, out.row_id, column_values)

    fields = {
        "mail_body": inp.mail_body or "",
        "subject": inp.subject or "",
        "from": inp.from_ or "",
        "from_name": inp.from_name or "",
        "geography": out.geography or "",
        "industry": out.industry or "",
        "client_name": out.client_name or "",
        "standards": out.standards or "",
        "title": out.title or "",
        "sequence": out.sequence or "",
        "raw_client_name": out.raw_client_name or "",
        "raw_model_output": out.raw_model_output or "",
        "structured": json.dumps(out.structured or {}, ensure_ascii=False),
        "writeback_enabled": str(bool(settings.enable_triage_writeback)),
    }

    rows = build_chunked_log_rows(
        settings=settings,
        run_id=out.run_id,
        mode=out.mode,
        row_id=out.row_id,
        fields=fields,
    )
    append_rows(settings, rows)


def write_regenerated_triage(
    settings: Settings,
    inp: RfqRegenerateTriageInputPayload,
    out: RfqRegenerateTriageOutputPayload,
) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    requested_at = inp.requested_time or generated_at

    if settings.enable_triage_writeback:
        required = {
            "GLIDE_COL_ZAI_REGENERATE_RFQ_ID": settings.glide_col_zai_regenerate_rfq_id,
            "GLIDE_COL_ZAI_REGENERATE_RESPONSE": settings.glide_col_zai_regenerate_response,
            "GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME": settings.glide_col_zai_regenerate_response_generated_time,
            "GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME": settings.glide_col_zai_regenerate_requested_time,
            "GLIDE_COL_ZAI_REGENERATE_INSTRUCTION": settings.glide_col_zai_regenerate_instruction,
            "GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY": settings.glide_col_zai_regenerate_requested_by,
            "GLIDE_COL_ZAI_REGENERATE_TYPE": settings.glide_col_zai_regenerate_type,
            "GLIDE_COL_ZAI_REGENERATE_VERSION": settings.glide_col_zai_regenerate_version,
        }
        missing = [name for name, value in required.items() if not (value or "").strip()]
        if missing:
            raise RuntimeError(f"Missing ZAI Regenerate writeback configuration: {', '.join(missing)}")

        glide_add_zai_regenerate_row(
            settings,
            {
                settings.glide_col_zai_regenerate_rfq_id: out.rfq_id,
                settings.glide_col_zai_regenerate_response: out.triage_text or "",
                settings.glide_col_zai_regenerate_response_generated_time: generated_at,
                settings.glide_col_zai_regenerate_requested_time: requested_at,
                settings.glide_col_zai_regenerate_instruction: out.instruction or "",
                settings.glide_col_zai_regenerate_requested_by: inp.requested_by or "",
                settings.glide_col_zai_regenerate_type: "instruction",
                settings.glide_col_zai_regenerate_version: inp.version or "",
            },
        )

    fields = {
        "rfq": json.dumps(inp.rfq or {}, ensure_ascii=False),
        "products": json.dumps(inp.products or [], ensure_ascii=False),
        "google_attachment_ids": json.dumps(inp.google_attachment_ids or [], ensure_ascii=False),
        "instruction": inp.instruction or "",
        "previous_instructions": json.dumps(inp.previous_instructions or [], ensure_ascii=False),
        "requested_by": inp.requested_by or "",
        "version": inp.version or "",
        "triage_text": out.triage_text or "",
        "raw_model_output": out.raw_model_output or "",
        "timings": json.dumps(out.timings or {}, ensure_ascii=False),
        "structured": json.dumps(out.structured or {}, ensure_ascii=False),
        "writeback_enabled": str(bool(settings.enable_triage_writeback)),
    }

    rows = build_chunked_log_rows(
        settings=settings,
        run_id=out.run_id,
        mode=out.mode,
        row_id=out.rfq_id,
        fields=fields,
    )
    append_rows(settings, rows)


def write_regenerated_query(
    settings: Settings,
    inp: RfqQueryInputPayload,
    out: RfqQueryOutputPayload,
) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    requested_at = inp.requested_time or generated_at

    if settings.enable_triage_writeback:
        required = {
            "GLIDE_COL_ZAI_REGENERATE_RFQ_ID": settings.glide_col_zai_regenerate_rfq_id,
            "GLIDE_COL_ZAI_REGENERATE_RESPONSE": settings.glide_col_zai_regenerate_response,
            "GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME": settings.glide_col_zai_regenerate_response_generated_time,
            "GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME": settings.glide_col_zai_regenerate_requested_time,
            "GLIDE_COL_ZAI_REGENERATE_INSTRUCTION": settings.glide_col_zai_regenerate_instruction,
            "GLIDE_COL_ZAI_REGENERATE_QUERY": settings.glide_col_zai_regenerate_query,
            "GLIDE_COL_ZAI_REGENERATE_TYPE": settings.glide_col_zai_regenerate_type,
            "GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY": settings.glide_col_zai_regenerate_requested_by,
            "GLIDE_COL_ZAI_REGENERATE_VERSION": settings.glide_col_zai_regenerate_version,
        }
        missing = [name for name, value in required.items() if not (value or "").strip()]
        if missing:
            raise RuntimeError(f"Missing ZAI Regenerate query writeback configuration: {', '.join(missing)}")

        glide_add_zai_regenerate_row(
            settings,
            {
                settings.glide_col_zai_regenerate_rfq_id: out.rfq_id,
                settings.glide_col_zai_regenerate_response: out.response_text or "",
                settings.glide_col_zai_regenerate_response_generated_time: generated_at,
                settings.glide_col_zai_regenerate_requested_time: requested_at,
                settings.glide_col_zai_regenerate_instruction: "",
                settings.glide_col_zai_regenerate_query: out.query or "",
                settings.glide_col_zai_regenerate_requested_by: inp.requested_by or "",
                settings.glide_col_zai_regenerate_type: "query",
                settings.glide_col_zai_regenerate_version: inp.version or "",
            },
        )

    fields = {
        "rfq": json.dumps(inp.rfq or {}, ensure_ascii=False),
        "products": json.dumps(inp.products or [], ensure_ascii=False),
        "google_attachment_ids": json.dumps(inp.google_attachment_ids or [], ensure_ascii=False),
        "query": inp.query or "",
        "previous_instructions": json.dumps(inp.previous_instructions or [], ensure_ascii=False),
        "requested_by": inp.requested_by or "",
        "version": inp.version or "",
        "response_text": out.response_text or "",
        "raw_model_output": out.raw_model_output or "",
        "timings": json.dumps(out.timings or {}, ensure_ascii=False),
        "structured": json.dumps(out.structured or {}, ensure_ascii=False),
        "writeback_enabled": str(bool(settings.enable_triage_writeback)),
    }

    rows = build_chunked_log_rows(
        settings=settings,
        run_id=out.run_id,
        mode=out.mode,
        row_id=out.rfq_id,
        fields=fields,
    )
    append_rows(settings, rows)
