from __future__ import annotations

import base64
import binascii
import html
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import quote

from .config import Settings
from .schema import InputPayload, OutputPayload, QueryPayload, TriageOutputPayload, RfqClassificationInputPayload, RfqClassificationOutputPayload, RfqRegenerateTriageInputPayload, RfqRegenerateTriageOutputPayload, RfqQueryInputPayload, RfqQueryOutputPayload
from .glide_client import glide_fetch_supplier_shares, glide_set_regenerate_response
from .glide_client import glide_upsert_zai_response_by_rfq_id, glide_update_all_rfq_triage_outputs, glide_update_prospect_rfq_classification, glide_add_zai_regenerate_row, glide_add_product_rows, glide_add_query_rows, glide_fetch_rfq_folder, glide_set_all_rfq_columns
from .costing_workbook import Commons, build_costing_workbook, tabs_from_extraction
from .onedrive import download_file, upload_file, upload_configured
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


# Render "Secret Files" land here. Dropping the master template in as a secret file
# named costing_template.xlsx is enough — no env var, and it never enters git.
# Render puts a Secret File in /etc/secrets/, and on native (non-Docker)
# services also in the app's root directory — both are checked.
DEFAULT_TEMPLATE_PATHS = ("/etc/secrets/costing_template.xlsx", "costing_template.xlsx")
_ILLEGAL_FILENAME = re.compile(r'[":<>?/\\|*\x00-\x1f]')


def _complete_workbook(data: bytes) -> bool:
    """A whole .xlsx, not just its first bytes: the zip opens and every part reads."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "[Content_Types].xml" in z.namelist() and z.testzip() is None
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def _as_workbook_bytes(raw: bytes):
    """
    The template as a complete .xlsx, whether it arrived as the file itself or
    as its base64 text — Render's Secret Files only take text, so the second is
    how it gets there. Returns (bytes or None, what happened) so a refusal can
    say *why*: a paste that was cut short still starts like a workbook, and
    without the reason that failure looks the same as a wrong file.
    """
    raw = raw or b""
    if raw[:2] == b"PK":
        if _complete_workbook(raw):
            return raw, ""
        return None, f"an .xlsx of {len(raw):,} bytes that is incomplete — upload it again"
    text = b"".join(raw.split())
    if not text:
        return None, "empty"
    # Base64 of any .xlsx starts "UEsD" (that is "PK\x03\x04"): anything else is not the template.
    if not text.startswith(b"UEsD") or not re.fullmatch(rb"[A-Za-z0-9+/]+={0,2}", text):
        return None, f"text of {len(text):,} characters that is neither an .xlsx nor base64 of one"
    if len(text) % 4:
        return None, (f"base64 text of {len(text):,} characters that stops mid-way — the paste was cut off "
                      f"(the full text is about 55,000 characters and ends in 'AAAAA')")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None, f"text of {len(text):,} characters that is neither an .xlsx nor base64 of one"
    if decoded[:2] != b"PK":
        return None, f"base64 text of {len(text):,} characters that is not an .xlsx"
    if not _complete_workbook(decoded):
        return None, (f"base64 text of {len(text):,} characters that decodes to an incomplete workbook — "
                      f"the paste was cut off (the full text is about 55,000 characters and ends in 'AAAAA')")
    return decoded, "decoded from base64"


def _share_download(settings: Settings, share_url: str):
    """A OneDrive / SharePoint share link -> the file's bytes, via Graph's /shares endpoint."""
    token = base64.urlsafe_b64encode(share_url.strip().encode()).decode().rstrip("=")
    data = download_file(settings, "", "", path=f"/shares/u!{token}/driveItem/content")
    return data


def _costing_template(settings: Settings, run_id: str):
    """
    The team's master workbook — the tabs a generated workbook is built into
    (Quotation, Back-end, insights). Looked for at the configured path, then as
    a Render secret file, then on OneDrive. Without it the workbook carries the
    generated tabs only, and the log says so loudly: that is the one case a
    reader of the uploaded file would otherwise just see as "tabs missing".
    """
    paths = [p for p in [(settings.costing_template_path or "").strip()] if p] + list(DEFAULT_TEMPLATE_PATHS)
    for path in paths:
        try:
            with open(path, "rb") as f:
                raw = f.read()
            data, note = _as_workbook_bytes(raw)
            if data:
                print(f"[INFO] run_id={run_id} | costing template: {path} ({len(data):,} bytes"
                      f"{', ' + note if note else ''})")
                return data
            print(f"[WARN] run_id={run_id} | costing template at {path} not used: it is {note}")
        except FileNotFoundError:
            if path == (settings.costing_template_path or "").strip():
                print(f"[WARN] run_id={run_id} | COSTING_TEMPLATE_PATH {path!r} does not exist")
        except OSError as e:
            print(f"[WARN] run_id={run_id} | costing template not readable at {path!r}: {e}")
    if (settings.costing_template_url or "").strip():
        data, note = _as_workbook_bytes(_share_download(settings, settings.costing_template_url) or b"")
        if data:
            print(f"[INFO] run_id={run_id} | costing template: from its OneDrive link ({len(data):,} bytes)")
            return data
        print(f"[WARN] run_id={run_id} | COSTING_TEMPLATE_URL did not give a usable workbook ({note or 'no file'}) "
              f"— check the link opens the .xlsx and the app can read that drive")
    if (settings.costing_template_item_id or "").strip():
        data = download_file(settings, settings.costing_template_drive_id or settings.ms_graph_drive_id,
                             settings.costing_template_item_id)
        if data:
            return data
        print(f"[WARN] run_id={run_id} | costing template could not be downloaded from OneDrive")
    print(f"[WARN] run_id={run_id} | NO COSTING TEMPLATE — the workbook will have the (Zai) tabs only, "
          f"without Quotation / Back-end / ExIm / Volza. Add it as a Render secret file named "
          f"costing_template.xlsx, or set COSTING_TEMPLATE_URL (a OneDrive share link to it).")
    return None


def workbook_sheet_names(data: bytes) -> List[str]:
    """Tab names straight from workbook.xml — cheap, no full load."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("xl/workbook.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return []
    return [html.unescape(n) for n in re.findall(r'<sheet\b[^>]*\bname="([^"]+)"', xml)]


def sheet_link(url: str, sheet: str) -> str:
    """
    The workbook's link, opening on one tab. Excel for the web reads
    `activeCell` on its Doc.aspx links — the form Graph returns for Office
    files — so the tab is selected when the link is opened.
    """
    url = (url or "").strip()
    if not url or not sheet:
        return url
    cell = quote(f"'{sheet}'!A1", safe="!")
    return f"{url}{'&' if '?' in url else '?web=1&'}activeCell={cell}"


def costing_workbook_filename(title: str, rfq_row_id: str) -> str:
    """`Int costing (New) - <RFQ title>.xlsx` — the team's naming, marked as generated."""
    name = re.sub(r"\s+", " ", _ILLEGAL_FILENAME.sub(" ", title or "")).strip(" .")[:80].rstrip(" .")
    return f"Int costing (New) - {name or rfq_row_id or 'RFQ'}.xlsx"


def _attach_costing_workbook(settings: Settings, out, rfq_row_id: str, extraction, rfq_title: str = "") -> bool:
    """
    Build the internal costing workbook for this RFQ, upload it to the RFQ's
    folder, and put its file id and link on the ALL RFQ row.

    Best-effort from end to end: no folder, no permission,
    a bad template or a Glide hiccup all leave the RFQ without a workbook link
    and never cost the extraction. Returns True only when the link was written.
    """
    if not settings.enable_costing_workbook or not upload_configured(settings):
        return False
    try:
        tabs = tabs_from_extraction(extraction)
    except Exception as e:
        print(f"[WARN] run_id={out.run_id} | costing tabs could not be built: {type(e).__name__}: {e}")
        return False
    if not tabs:
        return False
    try:
        drive_id, folder_id, row_title = glide_fetch_rfq_folder(settings, rfq_row_id)
    except Exception as e:
        print(f"[WARN] run_id={out.run_id} | costing workbook destination lookup failed: {type(e).__name__}: {e}")
        return False
    if not folder_id:
        print(f"[INFO] run_id={out.run_id} | no folder on the RFQ row — costing workbook not uploaded")
        return False

    commons = Commons(currency=settings.costing_currency, fx=settings.costing_fx_rate,
                      packaging=settings.costing_packaging, margin=settings.costing_margin,
                      pallet_capacity=settings.costing_pallet_capacity_kg,
                      price_per_pallet=settings.costing_price_per_pallet)
    template = _costing_template(settings, out.run_id)
    try:
        data = build_costing_workbook(tabs, template=template, commons=commons)
        if template:
            print(f"[INFO] run_id={out.run_id} | costing workbook built on the template")
    except Exception as e:
        if not template:
            print(f"[WARN] run_id={out.run_id} | costing workbook build failed: {type(e).__name__}: {e}")
            return False
        # A template we cannot edit must not cost the generated tabs.
        print(f"[WARN] run_id={out.run_id} | costing template rejected ({type(e).__name__}: {e}) — "
              f"building the generated tabs on their own")
        try:
            data = build_costing_workbook(tabs, commons=commons)
        except Exception as e2:
            print(f"[WARN] run_id={out.run_id} | costing workbook build failed: {type(e2).__name__}: {e2}")
            return False

    # The RFQ's own title: from its Glide row (QdiyR), else what the caller sent,
    # else the title the extraction wrote.
    header = getattr(extraction, "header", None)
    title = ((row_title or "").strip() or (rfq_title or "").strip()
             or str(getattr(header, "rfq_title", "") or getattr(header, "project", "") or "").strip())
    try:
        uploaded = upload_file(settings, drive_id, folder_id, costing_workbook_filename(title, rfq_row_id), data)
    except Exception as e:
        print(f"[WARN] run_id={out.run_id} | costing workbook upload raised: {type(e).__name__}: {e}")
        uploaded = None
    if not uploaded:
        return False
    columns = {
        settings.glide_col_all_rfq_costing_file_id: uploaded.id,
        settings.glide_col_all_rfq_costing_url: uploaded.url,
    }
    volza = (settings.costing_volza_sheet or "").strip()
    if uploaded.url and volza and volza in workbook_sheet_names(data):
        columns[settings.glide_col_all_rfq_volza_url] = sheet_link(uploaded.url, volza)
    try:
        glide_set_all_rfq_columns(settings, rfq_row_id, columns)
    except Exception as e:
        print(f"[WARN] run_id={out.run_id} | costing workbook uploaded but its link was not written: "
              f"{type(e).__name__}: {e}")
        return False
    lines = sum(len(t.lines) for t in tabs)
    print(f"[INFO] run_id={out.run_id} | costing workbook uploaded ({len(tabs)} tab(s), {lines} line(s), "
          f"template={'yes' if template else 'no'}) -> {uploaded.name}")
    return True


def grouping_summary(extraction: Any) -> str:
    """`3 line(s) for 27 item(s): 2 family (26 items), 1 single` — how far the extraction grouped."""
    products = getattr(extraction, "products", None) or []
    fam_lines = fam_items = singles = 0
    for p in products:
        if str(getattr(p, "structure", "") or "").lower() == "family":
            annexure = getattr(p, "annexure", None)
            n = getattr(p, "variant_count", None) or len(getattr(annexure, "rows", None) or []) or 1
            fam_lines, fam_items = fam_lines + 1, fam_items + n
        else:
            singles += 1
    parts = ([f"{fam_lines} family ({fam_items} items)"] if fam_lines else []) + ([f"{singles} single"] if singles else [])
    return f"{len(products)} line(s) for {fam_items + singles} item(s): " + ", ".join(parts)


def _write_extracted_products(settings: Settings, rfq_row_id: str, out: TriageOutputPayload, rfq_title: str = ""):
    """
    Adds the extracted product line items to the ALL Product table, then their open
    questions to the queries table, linked by the Row IDs Glide returns.

    Both are best-effort: the triage response is already stored by the time we get
    here, so a failure is logged and swallowed rather than failing the job.

    Returns (products_written, queries_written).
    """
    extraction = out.product_extraction
    if extraction is None or not extraction.products:
        why = "; ".join((extraction.parse_errors if extraction else None) or []) or "no product lines"
        print(f"[INFO] run_id={out.run_id} | no products extracted ({why}) — "
              f"no product rows written and no costing workbook built")
        return 0, 0

    print(f"[INFO] run_id={out.run_id} | product extraction grouped {grouping_summary(extraction)}")

    if not settings.enable_product_writeback:
        print(f"[INFO] run_id={out.run_id} | product writeback disabled; {len(extraction.products)} line(s) not written")
        return 0, 0

    if not (settings.glide_all_product_table or "").strip():
        print(
            f"[WARN] run_id={out.run_id} | GLIDE_ALL_PRODUCT_TABLE not configured; "
            f"{len(extraction.products)} product line(s) not written"
        )
        return 0, 0

    _attach_costing_workbook(settings, out, rfq_row_id, extraction, rfq_title)

    try:
        row_ids = glide_add_product_rows(settings, rfq_row_id, extraction.products)
    except Exception as e:
        print(f"[WARN] run_id={out.run_id} | product writeback failed: {type(e).__name__}: {e}")
        return 0, 0

    products_written = len(row_ids)
    resolved = {
        product.index: row_id
        for product, row_id in zip(extraction.products, row_ids)
        if product.index is not None and row_id
    }
    print(
        f"[INFO] run_id={out.run_id} | wrote {products_written} product row(s) to ALL Product "
        f"for rfq_id={rfq_row_id}; {len(resolved)}/{products_written} row id(s) resolved"
    )
    if products_written and not resolved:
        print(
            f"[WARN] run_id={out.run_id} | Glide returned no product row ids; "
            f"queries will be linked to the RFQ but not to their lines"
        )

    # Queries are written after the products so each one can carry its Product id.
    queries_written = 0
    if extraction.queries:
        customer = len(extraction.customer_queries())
        print(
            f"[INFO] run_id={out.run_id} | {customer} Customer / "
            f"{len(extraction.team_queries())} Team queries"
        )

        try:
            queries_written = glide_add_query_rows(settings, rfq_row_id, extraction.queries, resolved)
            print(f"[INFO] run_id={out.run_id} | wrote {queries_written} query row(s)")
        except Exception as e:
            print(f"[WARN] run_id={out.run_id} | query writeback failed: {type(e).__name__}: {e}")

    return products_written, queries_written


def write_products(settings: Settings, inp: QueryPayload, out: TriageOutputPayload) -> int:
    """
    Second phase of the triage job: add the extracted product line items and their
    open questions, and log them.

    Runs after write_triage, so the ZAI response is already in Glide by the time
    anything here happens.
    """
    products_written, queries_written = _write_extracted_products(settings, out.row_id, out,
                                                                  getattr(inp, "title", "") or "")

    rows = build_chunked_log_rows(
        settings=settings,
        run_id=out.run_id,
        mode="triage_products",
        row_id=out.row_id,
        fields=_product_log_fields(out, products_written, queries_written),
    )
    append_rows(settings, rows)
    return products_written


def write_past_quotes(settings: Settings, out: TriageOutputPayload) -> int:
    """
    Third phase of the triage job: the same or closely similar products quoted
    before, added at the end of the summary already in Glide.

    Best-effort and silent when there is nothing close: the summary only grows
    when a past quote is genuinely the same part or one size away. Returns the
    number of RFQ items that got a past reference.
    """
    from .past_quotes import attach_section, load_index, render_section, wanted_from_extraction

    if not settings.enable_past_quotes:
        return 0
    extraction = out.product_extraction
    if extraction is None or not extraction.products:
        print(f"[INFO] run_id={out.run_id} | past quotes | no products extracted — nothing to look up")
        return 0
    try:
        index = load_index(settings)
        if index is None:
            return 0
        wanted = wanted_from_extraction(extraction)
        results = [(w, index.find(w.fp, exclude_rfq=out.row_id, limit=max(1, settings.past_quotes_per_product)))
                   for w in wanted]
        found = [(w, ms) for w, ms in results if ms]
        same = sum(1 for _, ms in found if ms[0].level == "same")
        print(f"[INFO] run_id={out.run_id} | past quotes | {len(wanted)} item(s) looked up: "
              f"{same} quoted before, {len(found) - same} closely similar")
        if not found:
            return 0
        try:
            shares = glide_fetch_supplier_shares(settings, [m.line.rfq_row_id for _, ms in found for m in ms])
        except Exception as e:
            print(f"[WARN] run_id={out.run_id} | past quotes | suppliers not read: {type(e).__name__}: {e}")
            shares = {}
        text = attach_section(out.triage_text or "", render_section(found, shares))
        regenerate_row_id = str((out.structured or {}).get("regenerate_row_id") or "")
        if settings.enable_triage_writeback and regenerate_row_id:
            glide_set_regenerate_response(settings, regenerate_row_id, text)
        elif settings.enable_triage_writeback:
            print(f"[WARN] run_id={out.run_id} | past quotes | summary row id unknown — section not written")
        out.triage_text = text
        return len(found)
    except Exception as e:
        print(f"[WARN] run_id={out.run_id} | past quotes | skipped: {type(e).__name__}: {e}")
        return 0


def _variant_log_fields(products) -> Dict[str, str]:
    """
    Family variants are not written to Glide, so the sheet log is where they live.
    Rendered as TSV rather than JSON — a reviewer reads these, and 100 rows of JSON
    in one cell is unreadable.
    """
    lines: List[str] = []
    total = 0
    for product in products:
        annexure = product.annexure
        if not annexure or not annexure.rows:
            continue
        header = ["line", "product"] + [str(c) for c in (annexure.columns or [])]
        lines.append("\t".join(header))
        for row in annexure.rows:
            cells = row if isinstance(row, list) else [row.get(c, "") for c in (annexure.columns or [])] if isinstance(row, dict) else [row]
            lines.append("\t".join([str(product.index or ""), product.name] + [str(c) for c in cells]))
            total += 1

    if not total:
        return {"variants_extracted": "0"}
    return {"variants_extracted": str(total), "variants_tsv": "\n".join(lines)}


def _product_log_fields(out: TriageOutputPayload, products_written: int, queries_written: int) -> Dict[str, str]:
    extraction = out.product_extraction
    if extraction is None:
        return {
            "products_extracted": "0",
            "products_written": str(products_written),
            "products_json": "[]",
        }

    header = extraction.header
    summary = extraction.summary

    return {
        "products_extracted": str(len(extraction.products)),
        "products_written": str(products_written),
        "queries_extracted": str(len(extraction.queries)),
        "queries_customer": str(len(extraction.customer_queries())),
        "queries_team": str(len(extraction.team_queries())),
        "queries_written": str(queries_written),
        "products_json": json.dumps(
            [p.model_dump(mode="json") for p in extraction.products],
            ensure_ascii=False,
        ),
        "queries_json": json.dumps(
            [q.model_dump(mode="json") for q in extraction.queries],
            ensure_ascii=False,
        ),
        "products_header": json.dumps(header.model_dump(mode="json") if header else {}, ensure_ascii=False),
        "products_summary": json.dumps(summary.model_dump(mode="json") if summary else {}, ensure_ascii=False),
        "products_reconciliation": extraction.reconciliation_note(),
        **_variant_log_fields(extraction.products),
        "products_validation_warnings": json.dumps(extraction.validation_warnings or [], ensure_ascii=False),
        "products_skipped": json.dumps(extraction.skipped_products or [], ensure_ascii=False),
        "products_parse_errors": json.dumps(extraction.parse_errors or [], ensure_ascii=False),
        "raw_products_model_output": out.raw_products_model_output or "",
    }


def write_triage(settings: Settings, inp: QueryPayload, out: TriageOutputPayload) -> None:
    """
    Writes the initial triage output into the ZAI Regenerate table.
    Also logs to Sheets (chunked).
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    requested_at = inp.requested_time or generated_at
    requested_by = inp.requested_by or "system@wootz.work"

    if settings.enable_triage_writeback:
        required = {
            "GLIDE_COL_ZAI_REGENERATE_RFQ_ID": settings.glide_col_zai_regenerate_rfq_id,
            "GLIDE_COL_ZAI_REGENERATE_RESPONSE": settings.glide_col_zai_regenerate_response,
            "GLIDE_COL_ZAI_REGENERATE_RESPONSE_GENERATED_TIME": settings.glide_col_zai_regenerate_response_generated_time,
            "GLIDE_COL_ZAI_REGENERATE_REQUESTED_TIME": settings.glide_col_zai_regenerate_requested_time,
            "GLIDE_COL_ZAI_REGENERATE_REQUESTED_BY": settings.glide_col_zai_regenerate_requested_by,
            "GLIDE_COL_ZAI_REGENERATE_TYPE": settings.glide_col_zai_regenerate_type,
            "GLIDE_COL_ZAI_REGENERATE_VERSION": settings.glide_col_zai_regenerate_version,
        }
        missing = [name for name, value in required.items() if not (value or "").strip()]
        if missing:
            raise RuntimeError(f"Missing ZAI Regenerate triage writeback configuration: {', '.join(missing)}")

        regenerate_row_id = glide_add_zai_regenerate_row(
            settings,
            {
                settings.glide_col_zai_regenerate_rfq_id: out.row_id,
                settings.glide_col_zai_regenerate_response: out.triage_text or "",
                settings.glide_col_zai_regenerate_response_generated_time: generated_at,
                settings.glide_col_zai_regenerate_requested_time: requested_at,
                settings.glide_col_zai_regenerate_requested_by: requested_by,
                settings.glide_col_zai_regenerate_type: "instruction",
                settings.glide_col_zai_regenerate_version: "0",
            },
        )
        # Kept so the summary can be extended once the products are known (past quotes).
        out.structured = {**(out.structured or {}), "regenerate_row_id": regenerate_row_id or ""}
        glide_update_all_rfq_triage_outputs(
            settings,
            out.row_id,
            "",
            out.costing_estimate_text or "",
            out.costing_estimate_reason_text or "",
        )

    # Log to Sheets (same sheet schema; different field names)
    fields = {
        "subject": inp.subject,
        "from": inp.from_,
        "from_name": inp.from_name,
        "body": inp.body,
        "received_at": inp.received_at,
        "requested_time": inp.requested_time or "",
        "requested_by": inp.requested_by or "",
        "attachment_urls": json.dumps(inp.attachment_urls or [], ensure_ascii=False),  # replaces query_json
        "attached_media": json.dumps(inp.attached_media or [], ensure_ascii=False),
        "triage_text": out.triage_text or "",
        "costing_estimate_text": out.costing_estimate_text or "",
        "costing_estimate_reason_text": out.costing_estimate_reason_text or "",
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

    # Skip the Glide write only when we actually compared against a real
    # previous version and confirmed nothing material moved. A first-ever
    # regeneration (nothing to compare against) or a failed/skipped
    # comparison still writes — "couldn't check" must never be treated as
    # "confirmed unchanged."
    skip_write = out.compared and not out.changed
    if skip_write:
        print(f"[INFO] run_id={out.run_id} | response unchanged from previous version — Glide write skipped")

    if settings.enable_triage_writeback and not skip_write:
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
        glide_update_all_rfq_triage_outputs(
            settings,
            out.rfq_id,
            "",
            out.costing_estimate_text or "",
            out.costing_estimate_reason_text or "",
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
        "costing_estimate_text": out.costing_estimate_text or "",
        "costing_estimate_reason_text": out.costing_estimate_reason_text or "",
        "raw_model_output": out.raw_model_output or "",
        "raw_costing_model_output": out.raw_costing_model_output or "",
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
