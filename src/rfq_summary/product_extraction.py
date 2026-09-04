from __future__ import annotations

"""
Parsing for the RFQ product-extraction prompt (prompts/rfq_product_extraction.md).

The prompt emits NDJSON: an rfq_header, then each product followed immediately by
its own query objects, then the RFQ-level queries, then an rfq_summary. Kept
separate from task.py so it can be exercised without the LLM / attachment stack.

Several prompt rules are checked here rather than trusted to the model — the
prompt's own maintainer notes call for exactly that. Violations become
`validation_warnings`, which are logged and stored; they never block a write.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    PLACEHOLDER,
    QUERY_SECTIONS,
    ExtractedProduct,
    ExtractedQuery,
    ProductExtractionHeader,
    ProductExtractionResult,
    ProductExtractionSummary,
)

MAX_NAME_CHARS = 50

# §5.1 — a name that is only a number, or a pointer to somewhere else, is not a name.
FORBIDDEN_NAMES = {"test", "fastener", "as per attached excel", "as per drawing", "as per excel"}


def _strip_code_fences(text: str) -> str:
    """
    The prompt forbids fences, but models add them anyway. Drop fence lines and
    any prose before the first JSON object.
    """
    t = (text or "").strip()
    if not t:
        return ""

    lines = [ln for ln in t.splitlines() if not ln.strip().startswith("```")]
    t = "\n".join(lines).strip()

    idx = t.find("{")
    return t[idx:].strip() if idx > 0 else t


def parse_ndjson_objects(model_text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parse NDJSON leniently: one object per line is the contract, but a
    pretty-printed object spanning several lines is accepted too by buffering
    lines until the accumulated text parses.

    Returns (objects, parse_errors).
    """
    text = _strip_code_fences(model_text)
    if not text:
        return [], ["empty model output"]

    objects: List[Dict[str, Any]] = []
    errors: List[str] = []
    buffer: List[str] = []

    def flush(force: bool) -> None:
        if not buffer:
            return
        raw = "\n".join(buffer).strip().rstrip(",")
        if not raw:
            buffer.clear()
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            if force:
                errors.append(f"unparseable block ({e.msg}): {raw[:160]}")
                buffer.clear()
            return

        if isinstance(parsed, dict):
            objects.append(parsed)
        elif isinstance(parsed, list):
            objects.extend([it for it in parsed if isinstance(it, dict)])
        else:
            errors.append(f"ignored non-object JSON: {raw[:120]}")
        buffer.clear()

    for line in text.splitlines():
        if not line.strip():
            flush(force=False)
            continue
        buffer.append(line)
        flush(force=False)

    flush(force=True)
    return objects, errors


def _infer_object_type(obj: Dict[str, Any]) -> str:
    """"type" is occasionally omitted; infer it from the shape."""
    if "line_count_expected" in obj or "common_conditions" in obj or "rfq_title" in obj:
        return "rfq_header"
    if "placeholder_count" in obj or "notes_for_reviewer" in obj or "query_count" in obj:
        return "rfq_summary"
    if "description" in obj or "query_ref" in obj or "product_ref" in obj:
        return "query"
    if obj.get("name") or obj.get("Name") or obj.get("Product name"):
        return "product"
    return ""


def _validate(result: ProductExtractionResult) -> List[str]:
    """
    Check the prompt rules that are cheap to verify and expensive to miss.
    Returns human-readable warnings; never raises.
    """
    warnings: List[str] = []
    products = result.products
    queries = result.queries

    # §5.1 — name length and shape.
    for p in products:
        name = (p.name or "").strip()
        if len(name) > MAX_NAME_CHARS:
            warnings.append(f"line {p.index}: name is {len(name)} chars (max {MAX_NAME_CHARS}): {name[:60]!r}")
        if name.lower() in FORBIDDEN_NAMES or name.replace(" ", "").isdigit():
            warnings.append(f"line {p.index}: {name!r} is not a product name")

    # §8 — provenance is one token per field, never a phrase.
    for p in products:
        bad = p.bad_provenance_tokens()
        if bad:
            warnings.append(f"line {p.index}: provenance not a single token: {', '.join(bad[:4])}")

    # §5.3 — no sub-headings inside RFQ Details.
    for p in products:
        if "**" in (p.details or ""):
            warnings.append(f"line {p.index}: RFQ Details contains bold sub-headings")

    # §5.3 / §9 — every \-- maps to exactly one query row, and vice versa.
    placeholders = sum(p.placeholder_count() for p in products)
    if result.summary and result.summary.placeholder_count is not None:
        if result.summary.placeholder_count != placeholders:
            warnings.append(
                f"placeholder_count says {result.summary.placeholder_count} "
                f"but {placeholders} '\\--' markers are in the details"
            )
    if result.summary and result.summary.query_count is not None:
        if result.summary.query_count != len(queries):
            warnings.append(
                f"query_count says {result.summary.query_count} but {len(queries)} query objects were emitted"
            )

    rfq_level = [q for q in queries if q.is_rfq_level()]
    for p in products:
        if p.placeholder_count() and not result.queries_for(p.index) and not rfq_level:
            warnings.append(f"line {p.index}: has a '\\--' marker but no query row covers it")
    for p in products:
        if not p.placeholder_count() and result.queries_for(p.index):
            warnings.append(f"line {p.index}: has query rows but no '\\--' marker in the details")

    # §8 — the same question must not be asked twice.
    seen: Dict[str, ExtractedQuery] = {}
    for q in queries:
        key = " ".join((q.description or "").lower().split())
        if key and key in seen:
            warnings.append(f"duplicate query text: {(q.description or '')[:80]!r}")
        seen[key] = q

    # §1.2 — one question per row.
    for q in queries:
        if (q.description or "").count("?") > 1:
            warnings.append(f"query {q.query_ref or '?'} asks more than one question")

    # §9 — section must be one of the allowed tokens.
    for q in queries:
        section = (q.section or "").strip().lower()
        if section and section not in QUERY_SECTIONS:
            warnings.append(f"query {q.query_ref or '?'}: unknown section {section!r}")

    # A product carrying variants that never reach a table is worth flagging once.
    for p in products:
        if p.annexure and p.annexure.required and not p.annexure.by_reference and not p.annexure.rows:
            warnings.append(f"line {p.index}: annexure marked required but carries no variant rows")

    # A query pointing at a line that was never emitted cannot be linked on insert.
    known = {p.index for p in products if p.index is not None}
    for q in queries:
        if q.product_ref is not None and q.product_ref not in known:
            warnings.append(f"query {q.query_ref or '?'} refers to line {q.product_ref}, which was not extracted")

    return warnings


def _looks_truncated(model_text: str, errors: List[str]) -> bool:
    """
    An output cut off at the token cap ends mid-object: the last block fails to
    parse and the text does not close it. Distinguishable from ordinary junk.
    """
    if not any("unparseable block" in e for e in errors):
        return False
    tail = (model_text or "").rstrip()
    return bool(tail) and not tail.endswith(("}", "]"))


def parse_product_extraction(model_text: str) -> ProductExtractionResult:
    """
    Turn the product-extraction NDJSON into typed products and queries.

    Unnamed rows are dropped into skipped_products instead of the product table:
    a line with no part type is not quotable (prompt section 5.1).
    """
    objects, errors = parse_ndjson_objects(model_text)

    header: Optional[ProductExtractionHeader] = None
    summary: Optional[ProductExtractionSummary] = None
    products: List[ExtractedProduct] = []
    queries: List[ExtractedQuery] = []
    skipped: List[Dict[str, Any]] = []

    for obj in objects:
        kind = str(obj.get("type") or "").strip().lower() or _infer_object_type(obj)

        try:
            if kind == "rfq_header":
                header = ProductExtractionHeader.model_validate(obj)
            elif kind == "rfq_summary":
                summary = ProductExtractionSummary.model_validate(obj)
            elif kind == "query":
                query = ExtractedQuery.model_validate(obj)
                if query.is_emittable():
                    queries.append(query)
                else:
                    errors.append("query with no description dropped")
            elif kind == "product":
                product = ExtractedProduct.model_validate(obj)
                if product.is_emittable():
                    products.append(product)
                else:
                    skipped.append(obj)
            else:
                errors.append(f"unknown object type={kind!r}")
        except Exception as e:
            errors.append(f"{type(e).__name__} on type={kind or 'unknown'}: {e}")

    # Keep the customer's ordering; index is the customer-facing sequence.
    products.sort(key=lambda p: (p.index is None, p.index if p.index is not None else 0))

    result = ProductExtractionResult(
        header=header,
        products=products,
        queries=queries,
        summary=summary,
        skipped_products=skipped,
        parse_errors=errors,
        raw_model_output=model_text or "",
    )
    # Truncation is the one failure that silently costs a whole line item: the
    # product object is unterminated, so it never becomes a row. Name it plainly.
    if _looks_truncated(model_text, errors):
        result.parse_errors.append(
            "model output appears truncated — raise PRODUCT_EXTRACTION_MAX_TOKENS, or have the "
            "prompt carry large annexures by reference instead of inline"
        )

    result.validation_warnings = _validate(result)
    return result
