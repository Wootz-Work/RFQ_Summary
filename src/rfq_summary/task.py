from __future__ import annotations
import time
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple, List, Optional, Any
import re
from typing import Dict
from .config import Settings
from .schema import InputPayload, OutputPayload, WebFinding, QueryPayload, TriageOutputPayload, RfqClassificationInputPayload, RfqClassificationOutputPayload, RfqRegenerateTriageInputPayload, RfqRegenerateTriageOutputPayload, RfqQueryInputPayload, RfqQueryOutputPayload
from .attachments import analyze_attachments
from .search import PerplexitySearchClient
from .llm import load_prompt_file, generate_text
from .glide_client import glide_query_all_companies, glide_query_geographies, glide_query_industries


LEGAL_SUFFIX_RE = re.compile(
    r"\b("
    r"limited|ltd|llc|inc|gmbh|pvt\.?\s*ltd|private\s+limited|co\.?|company|"
    r"corporation|corp|plc|llp"
    r")\b\.?",
    flags=re.IGNORECASE,
)


def _join_attachment_text(payload: InputPayload, attachment_findings) -> str:
    # prefer externally provided extracted text if present
    if (payload.extracted_attachment_text or "").strip():
        return payload.extracted_attachment_text.strip()

    blocks: List[str] = []
    for a in attachment_findings:
        # Prefer rich extracted text from parsers (excel/pdf/image)
        extracted = ""
        try:
            extracted = (a.data or {}).get("extracted_text", "") or ""
        except Exception:
            extracted = ""

        if extracted.strip():
            blocks.append(f"[{a.kind}] {a.url}\n{extracted.strip()}\n")
        else:
            blocks.append(f"[{a.kind}] {a.url}\n{a.summary}\n")

    return "\n".join(blocks).strip()

def _join_attachment_text_any(external_extracted_text: str, attachment_findings) -> str:
    """
    Same semantics as _join_attachment_text but without needing an InputPayload-shaped object.
    """
    if (external_extracted_text or "").strip():
        return external_extracted_text.strip()

    blocks: List[str] = []
    for a in attachment_findings:
        extracted = ""
        try:
            extracted = (a.data or {}).get("extracted_text", "") or ""
        except Exception:
            extracted = ""

        if extracted.strip():
            blocks.append(f"[{a.kind}] {a.url}\n{extracted.strip()}\n")
        else:
            blocks.append(f"[{a.kind}] {a.url}\n{a.summary}\n")

    return "\n".join(blocks).strip()
def _parse_two_outputs(model_text: str) -> Tuple[str, str]:
    """
    Robustly parse OUTPUT 1 and OUTPUT 2.

    Handles common variants:
      - markdown headings like "## === OUTPUT 1: ..."
      - "OUTPUT 2" missing due to truncation
      - slightly different spacing/casing

    Returns:
      (out1, out2)
    """
    t = (model_text or "").strip()
    if not t:
        return "", ""

    # Normalize for searching, but slice on original string indices when possible
    t_low = t.lower()

    # Find OUTPUT 1 marker (best effort)
    candidates_1 = [
        "=== output 1",
        "## === output 1",
        "# === output 1",
        "output 1:",
        "output 1 -",
        "output 1 —",
    ]
    i1 = -1
    for c in candidates_1:
        j = t_low.find(c)
        if j >= 0:
            i1 = j
            break

    # Find OUTPUT 2 marker (best effort)
    candidates_2 = [
        "=== output 2",
        "## === output 2",
        "# === output 2",
        "output 2:",
        "output 2 -",
        "output 2 —",
    ]
    i2 = -1
    for c in candidates_2:
        j = t_low.find(c)
        if j >= 0:
            i2 = j
            break

    # If both markers found and ordered, split cleanly
    if i1 >= 0 and i2 > i1:
        out1 = t[i1:i2].strip()
        out2 = t[i2:].strip()
        return out1, out2

    # If OUTPUT 1 exists but OUTPUT 2 missing (truncated), treat everything after OUTPUT 1 as out1
    if i1 >= 0 and i2 < 0:
        out1 = t[i1:].strip()
        return out1, ""

    # If OUTPUT 2 exists but OUTPUT 1 missing, treat everything before OUTPUT 2 as out1 (rare)
    if i2 >= 0 and i1 < 0:
        out1 = t[:i2].strip()
        out2 = t[i2:].strip()
        return out1, out2

    # Fallback: no markers; return all as OUTPUT 2 (your previous behavior)
    return "", t


def _parse_single_output(model_text: str) -> str:
    t = (model_text or "").strip()
    if not t:
        return ""
    idx = t.find("=== OUTPUT")
    if idx >= 0:
        return t[idx:].strip()
    return t

def _parse_xml_sections(model_text: str) -> Dict[str, str]:
    """
    Extract required XML blocks:
      <summary>, <scope>, <cost>, <quality>, <timeline>

    Returns dict with keys: summary, scope, cost, quality, timeline
    Missing tags return "".
    """
    t = (model_text or "").strip()
    if not t:
        return {"summary": "", "scope": "", "cost": "", "quality": "", "timeline": ""}

    def grab(tag: str) -> str:
        # non-greedy capture between tags, allow multiline
        m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", t, flags=re.IGNORECASE | re.DOTALL)
        return (m.group(1).strip() if m else "")

    return {
        "summary": grab("summary"),
        "scope": grab("scope"),
        "cost": grab("cost"),
        "quality": grab("quality"),
        "timeline": grab("timeline"),
    }

def _extract_tag_block(text: str, tag: str) -> str:
    """
    Extract <tag>...</tag> content. Returns empty string if missing.
    """
    t = (text or "").strip()
    if not t:
        return ""
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", t, flags=re.IGNORECASE | re.DOTALL)
    return (m.group(1).strip() if m else "")


def _wrap_tagged_output(model_text: str, tag: str) -> str:
    inner = _extract_tag_block(model_text, tag)
    if inner:
        return f"<{tag}>\n{inner.strip()}\n</{tag}>"
    return f"<{tag}>\n{(model_text or '').strip()}\n</{tag}>"


def _unwrap_tagged_output(model_text: str, tag: str) -> str:
    t = (model_text or "").strip()
    if not t:
        return ""
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", t, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


def _generate_text_with_timing(settings: Settings, user_prompt: str) -> Tuple[str, int]:
    t_llm0 = time.perf_counter()
    model_text = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=user_prompt,
    )
    llm_ms = int((time.perf_counter() - t_llm0) * 1000)
    return model_text, llm_ms


def _parse_json_object(model_text: str) -> Dict[str, Any]:
    t = (model_text or "").strip()
    if not t:
        return {}
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _clean_client_name(name: str) -> str:
    cleaned = LEGAL_SUFFIX_RE.sub("", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" -_,.;:")
    return cleaned.strip()


def _company_options_for_classification(settings: Settings) -> List[Dict[str, str]]:
    pet_col = settings.glide_col_all_companies_pet_name
    original_col = settings.glide_col_all_companies_original_name
    options: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for row in glide_query_all_companies(settings):
        if not isinstance(row, dict):
            continue
        pet_name = _clean_client_name(str(row.get(pet_col) or ""))
        original_name = _clean_client_name(str(row.get(original_col) or ""))
        if not pet_name and not original_name:
            continue
        key = (pet_name.lower(), original_name.lower())
        if key in seen:
            continue
        seen.add(key)
        options.append(
            {
                "pet_name": pet_name,
                "original_name": original_name,
            }
        )

    return options


def _validated_pet_name_from_model(value: str, company_options: List[Dict[str, str]]) -> str:
    value_clean = _clean_client_name(value)
    if not value_clean:
        return ""

    for option in company_options:
        pet_name = str(option.get("pet_name") or "").strip()
        if pet_name and pet_name.lower() == value_clean.lower():
            return pet_name

    return ""


def _clean_lookup_value(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _dedupe_lookup_values(values: List[str]) -> List[str]:
    options: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_lookup_value(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        options.append(cleaned)
    return options


def _geography_options_for_classification(settings: Settings) -> List[str]:
    col = settings.glide_col_geographies_name
    return _dedupe_lookup_values(
        [str(row.get(col) or "") for row in glide_query_geographies(settings) if isinstance(row, dict)]
    )


def _industry_options_for_classification(settings: Settings) -> List[str]:
    col = settings.glide_col_industries_industry
    return _dedupe_lookup_values(
        [str(row.get(col) or "") for row in glide_query_industries(settings) if isinstance(row, dict)]
    )


def _validated_lookup_value_from_model(value: str, options: List[str]) -> str:
    value_clean = _clean_lookup_value(value)
    if not value_clean:
        return ""

    for option in options:
        if value_clean.lower() == option.lower():
            return option
    return ""


def _compose_rfq_title(client_name: str, title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip(" -")
    return title


def _products_for_prompt(payload: InputPayload) -> List[dict]:
    out: List[dict] = []
    products = getattr(payload, "products", None)
    if products:
        for p in products:
            out.append(
                {
                    "sr_no": p.sr_no,
                    "Name": p.name,
                    "Qty": p.qty,
                    "Details": p.details,
                    "Dwg": p.dwg,
                    "photo": p.photo,
                    "files": p.files,
                }
            )
    elif payload.product:
        p = payload.product
        out.append(
            {
                "sr_no": p.sr_no,
                "Name": p.name,
                "Qty": p.qty,
                "Details": p.details,
                "Dwg": p.dwg,
                "photo": p.photo,
                "files": p.files,
            }
        )
    return out


def _build_user_prompt(prompt_template: str, payload: InputPayload, extracted_text: str) -> str:
    rfq_json = {
        "Title": payload.title,
        "Industry": payload.industry,
        "Geography": payload.geography,
        "Standard": payload.standard,
        "Customer name": payload.customer_name,
        "Product_json": payload.product_json,        # raw traceability
        "Products": _products_for_prompt(payload),   # structured multi-product
        "rowID": payload.row_id,
    }

    s = prompt_template

    # New placeholders (preferred)
    s = s.replace("{{rfq_json}}", json.dumps(rfq_json, ensure_ascii=False))
    s = s.replace("{{extracted_attachment_text}}", extracted_text or "")

    # Backward-compatible placeholders (older prompts)
    s = s.replace("{{insert_main_rfq_json_here}}", json.dumps(rfq_json, ensure_ascii=False))
    s = s.replace("{{insert_extracted_text_from_power_automate_here}}", extracted_text or "")

    return s

def _build_query_triage_prompt(prompt_template: str, q: QueryPayload, extracted_text: str) -> str:
    query_dict = {
        "row_id": q.row_id,
        "subject": q.subject,
        "from_": q.from_,
        "from_name": q.from_name,
        "body": q.body,
        "received_at": q.received_at,
        "attachment_urls": q.attachment_urls,
        "attached_media": q.attached_media,
    }
    s = prompt_template
    s = s.replace("{{query_json}}", json.dumps(query_dict, ensure_ascii=False))
    s = s.replace("{{extracted_attachment_text}}", extracted_text or "")
    s = s.replace("{{attached_media}}", json.dumps(q.attached_media or [], ensure_ascii=False))
    return s

def _compact_product_text(payload: InputPayload) -> str:
    parts: List[str] = []
    products = getattr(payload, "products", None)
    if products:
        for p in products[:8]:
            s = f"{p.name}".strip()
            if p.qty:
                s += f" | Qty: {p.qty}"
            if p.details:
                d = p.details.replace("\n", " ").strip()
                s += f" | {d[:180]}"
            if s:
                parts.append(s)
        if len(products) > 8:
            parts.append(f"...(+{len(products) - 8} more items)")
        return " || ".join(parts)

    if payload.product:
        p = payload.product
        s = f"{p.name}".strip()
        if p.qty:
            s += f" | Qty: {p.qty}"
        if p.details:
            d = p.details.replace("\n", " ").strip()
            s += f" | {d[:180]}"
        return s
    return ""

def _aggregate_docai_stats(attachment_findings) -> Dict[str, Any]:
    used = False
    pdf_files = 0
    docai_pages = 0
    failed_pdfs = 0

    for a in attachment_findings or []:
        if (a.kind or "") != "pdf":
            continue
        pdf_files += 1
        d = (a.data or {}) if hasattr(a, "data") else {}
        if bool(d.get("docai_used")):
            used = True
            try:
                docai_pages += int(d.get("docai_pages_used") or d.get("docai_pages_returned") or 0)
            except Exception:
                pass
        else:
            # if docai not used and error present, count as failed for visibility
            if (d.get("docai_error") or "").strip():
                failed_pdfs += 1

    return {
        "used": used,
        "pdf_files": pdf_files,
        "pages": docai_pages,
        "failed_pdfs": failed_pdfs,
    }

def run_pricing(settings: Settings, payload: InputPayload, run_id: Optional[str] = None) -> OutputPayload:
    run_id = run_id or uuid.uuid4().hex[:10]
    t0 = time.perf_counter()
    t_attach0 = time.perf_counter()
    attachment_findings = analyze_attachments(settings, payload.all_attachment_urls())
    extracted_text = _join_attachment_text(payload, attachment_findings)
    attachments_ms = int((time.perf_counter() - t_attach0) * 1000)

    prompt_template = load_prompt_file(settings.prompt_pricing_file)

    q = (
        f"Wholesale unit pricing India for RFQ: {payload.title} | {payload.standard} | "
        f"{_compact_product_text(payload)}"
    )
    t_web0 = time.perf_counter()
    web_findings: List[WebFinding] = PerplexitySearchClient(settings).search(q)
    web_ms = int((time.perf_counter() - t_web0) * 1000)

    user_prompt = _build_user_prompt(prompt_template, payload, extracted_text)
    if web_findings:
        user_prompt += "\n\n[WEB_FINDINGS]\n" + "\n".join([f"- {w.title} {w.url}\n{w.snippet}" for w in web_findings])

    t_llm0 = time.perf_counter()
    model_text = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=user_prompt,
    )
    llm_ms = int((time.perf_counter() - t_llm0) * 1000)
    out1, out2 = _parse_two_outputs(model_text)

    first = payload.product
    products = getattr(payload, "products", None) or []
    total_ms = int((time.perf_counter() - t0) * 1000)
    docai = _aggregate_docai_stats(attachment_findings)
    return OutputPayload(
        run_id=run_id,
        mode="pricing",
        row_id=payload.row_id,
        rfq_title=payload.title,
        customer_name=payload.customer_name,
        standard=payload.standard,
        geography=payload.geography,
        industry=payload.industry,
        product_name=(first.name if first else (f"{len(products)} item(s)" if products else "")),
        product_qty=(first.qty if first else ""),
        product_details=(first.details if first else ""),
        attachment_findings=attachment_findings,
        web_findings=web_findings,
        pricing_estimate_text=out1,
        pricing_reasoning_text=out2,
        raw_model_output=model_text,
        structured={"products_count": (len(products) if products else (1 if first else 0))},
        timings={
            "attachments_ms": attachments_ms,
            "web_ms": web_ms,
            "llm_ms": llm_ms,
            "total_ms": total_ms,
        },
        docai=docai,
    )


def run_summary(settings: Settings, payload: InputPayload, run_id: Optional[str] = None) -> OutputPayload:
    run_id = run_id or uuid.uuid4().hex[:10]
    t0 = time.perf_counter()
    t_attach0 = time.perf_counter()
    attachment_findings = analyze_attachments(settings, payload.all_attachment_urls())
    extracted_text = _join_attachment_text(payload, attachment_findings)
    attachments_ms = int((time.perf_counter() - t_attach0) * 1000)
    prompt_template = load_prompt_file(settings.prompt_summary_file)

    q = (
        f"India supplier clusters and cost proxy guidance for RFQ: {payload.title} | {payload.standard} | "
        f"{_compact_product_text(payload)}"
    )
    t_web0 = time.perf_counter()
    web_findings: List[WebFinding] = PerplexitySearchClient(settings).search(q)
    web_ms = int((time.perf_counter() - t_web0) * 1000)

    user_prompt = _build_user_prompt(prompt_template, payload, extracted_text)
    if web_findings:
        user_prompt += "\n\n[WEB_FINDINGS]\n" + "\n".join([f"- {w.title} {w.url}\n{w.snippet}" for w in web_findings])

    t_llm0 = time.perf_counter()
    model_text = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=user_prompt,
    )
    llm_ms = int((time.perf_counter() - t_llm0) * 1000)

    sections = _parse_xml_sections(model_text)

    first = payload.product
    products = getattr(payload, "products", None) or []
    total_ms = int((time.perf_counter() - t0) * 1000)
    docai = _aggregate_docai_stats(attachment_findings)
    return OutputPayload(
        run_id=run_id,
        mode="summary",
        row_id=payload.row_id,
        rfq_title=payload.title,
        customer_name=payload.customer_name,
        standard=payload.standard,
        geography=payload.geography,
        industry=payload.industry,
        product_name=(first.name if first else (f"{len(products)} item(s)" if products else "")),
        product_qty=(first.qty if first else ""),
        product_details=(first.details if first else ""),
        attachment_findings=attachment_findings,
        web_findings=web_findings,
        pricing_estimate_text="",
        pricing_reasoning_text="",
        summary_text=sections.get("summary", ""),
        scope_text=sections.get("scope", ""),
        cost_text=sections.get("cost", ""),
        quality_text=sections.get("quality", ""),
        timeline_text=sections.get("timeline", ""),
        raw_model_output=model_text,
        timings={
            "attachments_ms": attachments_ms,
            "web_ms": web_ms,
            "llm_ms": llm_ms,
            "total_ms": total_ms,
        },
        docai=docai,
        structured={
            "products_count": (len(products) if products else (1 if first else 0)),
            "xml_ok": bool(sections.get("scope") or sections.get("cost") or sections.get("quality") or sections.get("timeline")),
        }
    )

def run_all(settings: Settings, payload: InputPayload, run_id: Optional[str] = None) -> OutputPayload:
    """
    Run pricing + summary in ONE job.
    - Attachments are downloaded/parsed ONCE.
    - Websearch happens twice (pricing query, summary query) as requested.
    - Two separate Claude calls (two prompts).
    - Returns a single OutputPayload with all three output fields populated.
    """
    run_id = run_id or uuid.uuid4().hex[:10]

    # 1) Parse attachments ONCE
    attachment_findings = analyze_attachments(settings, payload.all_attachment_urls())
    extracted_text = _join_attachment_text(payload, attachment_findings)

    # 2) Pricing web search + pricing prompt
    pricing_prompt_template = load_prompt_file(settings.prompt_pricing_file)
    q_pricing = (
        f"Wholesale unit pricing India for RFQ: {payload.title} | {payload.standard} | "
        f"{_compact_product_text(payload)}"
    )
    web_pricing: List[WebFinding] = PerplexitySearchClient(settings).search(q_pricing)

    pricing_user_prompt = _build_user_prompt(pricing_prompt_template, payload, extracted_text)
    if web_pricing:
        pricing_user_prompt += "\n\n[WEB_FINDINGS]\n" + "\n".join(
            [f"- {w.title} {w.url}\n{w.snippet}" for w in web_pricing]
        )

    pricing_model_text = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=pricing_user_prompt,
    )
    out1, out2 = _parse_two_outputs(pricing_model_text)

    # 3) Summary web search + summary prompt
    summary_prompt_template = load_prompt_file(settings.prompt_summary_file)
    q_summary = (
        f"India supplier clusters and cost proxy guidance for RFQ: {payload.title} | {payload.standard} | "
        f"{_compact_product_text(payload)}"
    )
    web_summary: List[WebFinding] = PerplexitySearchClient(settings).search(q_summary)

    summary_user_prompt = _build_user_prompt(summary_prompt_template, payload, extracted_text)
    if web_summary:
        summary_user_prompt += "\n\n[WEB_FINDINGS]\n" + "\n".join(
            [f"- {w.title} {w.url}\n{w.snippet}" for w in web_summary]
        )

    summary_model_text = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=summary_user_prompt,
    )
    sections = _parse_xml_sections(summary_model_text)

    # 4) Build combined output
    first = payload.product
    products = getattr(payload, "products", None) or []

    # merge web findings (writer logs one list; we keep both sets)
    merged_web = []
    for w in web_pricing:
        merged_web.append(WebFinding(title=f"[pricing] {w.title}", url=w.url, snippet=w.snippet))
    for w in web_summary:
        merged_web.append(WebFinding(title=f"[summary] {w.title}", url=w.url, snippet=w.snippet))

    combined_raw = (
        "=== PRICING_MODEL_OUTPUT ===\n"
        + (pricing_model_text or "")
        + "\n\n=== SUMMARY_MODEL_OUTPUT ===\n"
        + (summary_model_text or "")
    )

    return OutputPayload(
        run_id=run_id,
        mode="all",
        row_id=payload.row_id,
        rfq_title=payload.title,
        customer_name=payload.customer_name,
        standard=payload.standard,
        geography=payload.geography,
        industry=payload.industry,
        product_name=(first.name if first else (f"{len(products)} item(s)" if products else "")),
        product_qty=(first.qty if first else ""),
        product_details=(first.details if first else ""),
        attachment_findings=attachment_findings,
        web_findings=merged_web,
        pricing_estimate_text=out1,
        pricing_reasoning_text=out2,
        summary_text=sections.get("summary", ""),
        scope_text=sections.get("scope", ""),
        cost_text=sections.get("cost", ""),
        quality_text=sections.get("quality", ""),
        timeline_text=sections.get("timeline", ""),
        raw_model_output=combined_raw,
        structured={"products_count": (len(products) if products else (1 if first else 0))},
    )

def run_query_triage(settings: Settings, payload: QueryPayload, run_id: Optional[str] = None) -> TriageOutputPayload:
    run_id = run_id or uuid.uuid4().hex[:10]
    t0 = time.perf_counter()

    # 1) attachments parse (same pipeline; safe caps + DocAI)
    t_attach0 = time.perf_counter()
    attachment_findings = analyze_attachments(settings, payload.all_attachment_urls())
    extracted_text = _join_attachment_text_any("", attachment_findings)
    attachments_ms = int((time.perf_counter() - t_attach0) * 1000)

    # 2) Build both prompts from the same parsed attachment context.
    triage_prompt_template = load_prompt_file(settings.prompt_query_triage_file)
    costing_prompt_template = load_prompt_file(settings.prompt_query_costing_file)
    triage_user_prompt = _build_query_triage_prompt(triage_prompt_template, payload, extracted_text)
    costing_user_prompt = _build_query_triage_prompt(costing_prompt_template, payload, extracted_text)

    # 3) Run both Claude calls in parallel.
    with ThreadPoolExecutor(max_workers=2) as executor:
        triage_future = executor.submit(_generate_text_with_timing, settings, triage_user_prompt)
        costing_future = executor.submit(_generate_text_with_timing, settings, costing_user_prompt)
        model_text, triage_llm_ms = triage_future.result()
        costing_model_text, costing_llm_ms = costing_future.result()

    triage_text = _wrap_tagged_output(model_text, "triage")
    costing_estimate_text = _unwrap_tagged_output(costing_model_text, "estimate")

    # DocAI stats (reuse your existing aggregator)
    docai = _aggregate_docai_stats(attachment_findings)
    total_ms = int((time.perf_counter() - t0) * 1000)

    return TriageOutputPayload(
        run_id=run_id,
        row_id=payload.row_id,
        triage_text=triage_text,
        costing_estimate_text=costing_estimate_text,
        raw_model_output=model_text or "",
        raw_costing_model_output=costing_model_text or "",
        attachment_findings=attachment_findings,
        timings={
            "attachments_ms": attachments_ms,
            "triage_llm_ms": triage_llm_ms,
            "costing_llm_ms": costing_llm_ms,
            "llm_parallel_max_ms": max(triage_llm_ms, costing_llm_ms),
            "total_ms": total_ms,
        },
        docai=docai,
        structured={"attachments_count": len(attachment_findings or [])},
    )


def run_rfq_classification(
    settings: Settings,
    payload: RfqClassificationInputPayload,
    run_id: Optional[str] = None,
) -> RfqClassificationOutputPayload:
    run_id = run_id or uuid.uuid4().hex[:10]
    prompt_template = load_prompt_file(settings.prompt_query_rfq_classification_file)
    mail_dict = {
        "row_id": payload.row_id,
        "subject": payload.subject,
        "from_": payload.from_,
        "from_name": payload.from_name,
        "mail_body": payload.mail_body,
    }
    company_options = _company_options_for_classification(settings)
    geography_options = _geography_options_for_classification(settings)
    industry_options = _industry_options_for_classification(settings)
    user_prompt = (
        prompt_template
        .replace("{{mail_json}}", json.dumps(mail_dict, ensure_ascii=False))
        .replace("{{companies_json}}", json.dumps(company_options, ensure_ascii=False))
        .replace("{{geographies_json}}", json.dumps(geography_options, ensure_ascii=False))
        .replace("{{industries_json}}", json.dumps(industry_options, ensure_ascii=False))
    )
    raw_model_output = generate_text(
        settings,
        system_prompt="You must return valid JSON only.",
        user_prompt=user_prompt,
    )
    parsed = _parse_json_object(raw_model_output)

    raw_client_name = _clean_client_name(str(parsed.get("client_name") or ""))
    client_name = _validated_pet_name_from_model(raw_client_name, company_options)
    title = _compose_rfq_title(client_name, str(parsed.get("title") or ""))

    return RfqClassificationOutputPayload(
        run_id=run_id,
        row_id=payload.row_id,
        geography=_validated_lookup_value_from_model(str(parsed.get("geography") or ""), geography_options),
        industry=_validated_lookup_value_from_model(str(parsed.get("industry") or ""), industry_options),
        client_name=client_name,
        standards=str(parsed.get("standards") or "").strip(),
        title=title,
        sequence="",
        raw_client_name=raw_client_name,
        raw_model_output=raw_model_output or "",
        structured={
            "company_matched": bool(client_name),
            "matched_company_pet_name": client_name,
            "companies_count": len(company_options),
            "geographies_count": len(geography_options),
            "industries_count": len(industry_options),
        },
    )


def run_regenerate_triage(
    settings: Settings,
    payload: RfqRegenerateTriageInputPayload,
    run_id: Optional[str] = None,
) -> RfqRegenerateTriageOutputPayload:
    run_id = run_id or uuid.uuid4().hex[:10]
    t0 = time.perf_counter()

    t_attach0 = time.perf_counter()
    attachment_findings = analyze_attachments(settings, payload.google_attachment_ids or [])
    extracted_text = _join_attachment_text_any("", attachment_findings)
    attachments_ms = int((time.perf_counter() - t_attach0) * 1000)

    rfq = payload.rfq or {}
    prompt_body = {
        "rfq": rfq,
        "products": payload.products or [],
    }
    query_payload = QueryPayload(
        row_id=payload.rfq_id,
        subject=str(rfq.get("title") or ""),
        from_="",
        from_name=str(rfq.get("customer_name") or ""),
        body=json.dumps(prompt_body, ensure_ascii=False),
        received_at="",
        attachment_urls=payload.google_attachment_ids or [],
        attached_media=[],
    )

    base_triage_prompt = load_prompt_file(settings.prompt_query_triage_file)
    regenerate_prompt_template = load_prompt_file(settings.prompt_query_regenerate_triage_file)
    previous_instructions_json = json.dumps(payload.previous_instructions or [], ensure_ascii=False)
    prompt_template = (
        regenerate_prompt_template
        .replace("{{base_triage_prompt}}", base_triage_prompt)
        .replace("{{previous_instructions}}", previous_instructions_json)
        .replace("{{current_instruction}}", (payload.instruction or "").strip())
    )
    user_prompt = _build_query_triage_prompt(prompt_template, query_payload, extracted_text)
    t_llm0 = time.perf_counter()
    model_text = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=user_prompt,
    )
    triage_llm_ms = int((time.perf_counter() - t_llm0) * 1000)
    total_ms = int((time.perf_counter() - t0) * 1000)

    return RfqRegenerateTriageOutputPayload(
        run_id=run_id,
        rfq_id=payload.rfq_id,
        instruction=payload.instruction or "",
        triage_text=_wrap_tagged_output(model_text, "triage"),
        raw_model_output=model_text or "",
        attachment_findings=attachment_findings,
        timings={
            "attachments_ms": attachments_ms,
            "triage_llm_ms": triage_llm_ms,
            "total_ms": total_ms,
        },
        structured={
            "attachments_count": len(attachment_findings or []),
            "products_count": len(payload.products or []),
        },
    )


def run_regenerate_query(
    settings: Settings,
    payload: RfqQueryInputPayload,
    run_id: Optional[str] = None,
) -> RfqQueryOutputPayload:
    run_id = run_id or uuid.uuid4().hex[:10]
    t0 = time.perf_counter()

    t_attach0 = time.perf_counter()
    attachment_findings = analyze_attachments(settings, payload.google_attachment_ids or [])
    extracted_text = _join_attachment_text_any("", attachment_findings)
    attachments_ms = int((time.perf_counter() - t_attach0) * 1000)

    context = {
        "rfq": payload.rfq or {},
        "products": payload.products or [],
    }
    previous_instructions_json = json.dumps(payload.previous_instructions or [], ensure_ascii=False)
    prompt_template = load_prompt_file(settings.prompt_query_regenerate_file)
    user_prompt = (
        prompt_template
        .replace("{{user_query}}", (payload.query or "").strip())
        .replace("{{previous_instructions}}", previous_instructions_json)
        .replace("{{rfq_json}}", json.dumps(context, ensure_ascii=False))
        .replace("{{extracted_attachment_text}}", extracted_text or "")
    )

    t_llm0 = time.perf_counter()
    model_text = generate_text(
        settings,
        system_prompt="You must follow the user instructions exactly.",
        user_prompt=user_prompt,
    )
    query_llm_ms = int((time.perf_counter() - t_llm0) * 1000)
    total_ms = int((time.perf_counter() - t0) * 1000)

    return RfqQueryOutputPayload(
        run_id=run_id,
        rfq_id=payload.rfq_id,
        query=payload.query or "",
        response_text=model_text or "",
        raw_model_output=model_text or "",
        attachment_findings=attachment_findings,
        timings={
            "attachments_ms": attachments_ms,
            "query_llm_ms": query_llm_ms,
            "total_ms": total_ms,
        },
        structured={
            "attachments_count": len(attachment_findings or []),
            "products_count": len(payload.products or []),
        },
    )
