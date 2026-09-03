from __future__ import annotations

"""
Parsing for the RFQ product-extraction prompt (prompts/rfq_product_extraction.md).

The prompt emits NDJSON: an rfq_header object, then one product object per line
item in customer order, then an rfq_summary object. Kept separate from task.py so
it can be exercised without the LLM / attachment stack.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    ExtractedProduct,
    ProductExtractionHeader,
    ProductExtractionResult,
    ProductExtractionSummary,
)


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
    if "line_count_expected" in obj or "rfq_title" in obj:
        return "rfq_header"
    if "placeholder_count" in obj or "notes_for_reviewer" in obj:
        return "rfq_summary"
    if obj.get("name") or obj.get("Name"):
        return "product"
    return ""


def parse_product_extraction(model_text: str) -> ProductExtractionResult:
    """
    Turn the product-extraction NDJSON into typed rows.

    Unnamed rows are dropped into skipped_products instead of the product table:
    a line with no part type is not quotable (prompt section 5.1).
    """
    objects, errors = parse_ndjson_objects(model_text)

    header: Optional[ProductExtractionHeader] = None
    summary: Optional[ProductExtractionSummary] = None
    products: List[ExtractedProduct] = []
    skipped: List[Dict[str, Any]] = []

    for obj in objects:
        kind = str(obj.get("type") or "").strip().lower() or _infer_object_type(obj)

        try:
            if kind == "rfq_header":
                header = ProductExtractionHeader.model_validate(obj)
            elif kind == "rfq_summary":
                summary = ProductExtractionSummary.model_validate(obj)
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

    return ProductExtractionResult(
        header=header,
        products=products,
        summary=summary,
        skipped_products=skipped,
        parse_errors=errors,
        raw_model_output=model_text or "",
    )
