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
import re
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
# §1.2 — a customer reads these. Four is the ceiling for a whole RFQ.
MAX_QUERIES_PER_RFQ = 4

# §1.2 — four kinds of question that must never reach a customer. Checked here
# because a single bad query is visible to the customer the moment it is sent.
BANNED_QUERY_PATTERNS: List[Tuple[str, str]] = [
    # Our own tooling problems.
    (r"\b(could\s?n[o']t|cannot|can'?t|unable to|failed to|did\s?n[o']t|would\s?n[o']t|will not|wo\s?n't|does\s?n[o']t|is not)\s+(open|opening|read|readable|access|accessible|download|fetch|extract)\b",
     "asks the customer about a file we could not open — ours to chase"),
    # "share it again" implies we had it and lost it. Asking for something never
    # sent ("the drawing is referenced but not attached") stays legitimate.
    (r"\b(re-?send|resend|send (it |them )?again|share (it |them |the \w+ )?again|forward (it |them )?again)\b",
     "asks the customer to resend something we already had — ours to chase"),
    (r"\b(corrupt|unreadable|blank|empty)\b.{0,25}\b(file|attachment|pdf|sheet)\b",
     "asks the customer about an unreadable file — ours to chase"),
    # Our internal overhead — administrative, whatever reason is given.
    (r"\b(project|programme|program)\s+(or\s+\w+\s+)?(name|title|number|reference|code)\b",
     "asks for a project name or reference — administrative, note it for the reviewer instead"),
    (r"\b(reference|enquiry|rfq)\s*(number|no\.?|code|id)\b",
     "asks for a reference number — administrative"),
    (r"\bfor (our|internal) (record|records|tracking|reference|system|purposes)\b",
     "asks for something for our own records"),
    (r"\btrack (it|this) internally\b",
     "asks for something for our own tracking"),
    # Commercial terms: we hold these already.
    (r"\b(currency|incoterm|inco-?term|payment terms|delivery address|billing)\b",
     "asks a commercial term — we hold these, they are never asked"),
    # The supplier model is ours, not theirs.
    (r"\b(supplier|suppliers|vendor|vendors|sub-?contractor|partner factory|our factory partner)\b",
     "names a supplier or vendor — to the customer we are the manufacturer"),
    # On the assume list. A genuine contradiction ("the drawing says Level 3 but the
    # email says Level 2") reads differently and is not caught by this.
    (r"\b(ppap|first[- ]article|fai|isir)\b.{0,50}\b(required|require|needed|need|applicable|apply|which level|what level|level\?)",
     "asks whether PPAP applies — assumed not included and quotable separately"),
    # On the assume list.
    (r"\b(annual|one-?time|blanket|per year|every \d+ months?)\b.{0,60}\b(basis|usage|requirement|quantit)",
     "asks for quantity basis, which is assumed and covered in the quote"),
    (r"\b(quantit\w+|volume)\b.{0,40}\b(one-?time|annual|recurring|repeat|blanket)\b",
     "asks for quantity basis, which is assumed and covered in the quote"),
]

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

    # §1.2 — at most four questions go to a customer for the whole RFQ.
    if len(queries) > MAX_QUERIES_PER_RFQ:
        warnings.append(
            f"{len(queries)} queries emitted; the cap is {MAX_QUERIES_PER_RFQ} for the whole RFQ — "
            f"keep the ones with the largest price impact and drop the rest"
        )

    # §8 — the same question must not be asked twice, verbatim or reworded. One
    # question covering several lines is one row carrying all their indexes.
    seen: Dict[str, ExtractedQuery] = {}
    for q in queries:
        key = " ".join((q.description or "").lower().split())
        if key and key in seen:
            warnings.append(f"duplicate query text: {(q.description or '')[:80]!r}")
        seen[key] = q

    for a, b in _near_duplicate_pairs(queries):
        warnings.append(
            f"queries {a.query_ref or '?'} and {b.query_ref or '?'} ask the same thing in different words — "
            f"merge into one row covering lines {sorted(set(a.product_refs) | set(b.product_refs))}"
        )

    # §1.2 — one question per row.
    for q in queries:
        if (q.description or "").count("?") > 1:
            warnings.append(f"query {q.query_ref or '?'} asks more than one question")

    # §1.2 — questions that must never be put to a customer.
    for q in queries:
        text = " ".join((q.description or "").split())
        for pattern, reason in BANNED_QUERY_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                warnings.append(f"query {q.query_ref or '?'} {reason}: {text[:90]!r}")
                break

    # §9 — section must be one of the allowed tokens.
    for q in queries:
        section = (q.section or "").strip().lower()
        if section == "commercial":
            warnings.append(
                f"query {q.query_ref or '?'} is a commercial query — currency, incoterm and "
                f"payment terms are held in our systems and are never asked"
            )
        elif section and section not in QUERY_SECTIONS:
            warnings.append(f"query {q.query_ref or '?'}: unknown section {section!r}")

    # A product carrying variants that never reach a table is worth flagging once.
    for p in products:
        if p.annexure and p.annexure.required and not p.annexure.by_reference and not p.annexure.rows:
            warnings.append(f"line {p.index}: annexure marked required but carries no variant rows")

    # A query pointing at a line that was never emitted cannot be linked on insert.
    known = {p.index for p in products if p.index is not None}
    for q in queries:
        missing = [ref for ref in q.product_refs if ref not in known]
        if missing:
            warnings.append(
                f"query {q.query_ref or '?'} refers to line(s) {missing}, which were not extracted"
            )

    return warnings


# Words that carry no signal when comparing two questions for sameness.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "and", "or", "of", "to", "for", "on", "in", "at", "we",
    "you", "your", "our", "us", "it", "this", "these", "those", "that", "be", "please",
    "confirm", "which", "what", "if", "so", "with", "have", "has", "would", "should",
    "could", "can", "let", "know", "any", "applies", "apply", "required", "there",
}


def _significant_words(text: str) -> set:
    words = (w.strip(".,;:") for w in re.findall(r"[a-z0-9.\-]{3,}", (text or "").lower()))
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _near_duplicate_pairs(queries: List[ExtractedQuery]):
    """
    Two questions asking the same thing in different words — the failure a
    verbatim-match check misses, and the reason one question gets asked per line
    instead of once across several.
    """
    pairs = []
    for i, a in enumerate(queries):
        words_a = _significant_words(a.description)
        if len(words_a) < 3:
            continue
        for b in queries[i + 1 :]:
            if (a.section or "").strip().lower() != (b.section or "").strip().lower():
                continue
            words_b = _significant_words(b.description)
            if len(words_b) < 3:
                continue
            # Containment against the shorter question, so a long restatement of a
            # short one is still caught. Measured on real output, the same question
            # reworded scores ~0.6 while genuinely different ones score ~0.1.
            containment = len(words_a & words_b) / min(len(words_a), len(words_b))
            if containment >= 0.5:
                pairs.append((a, b))
    return pairs


def _looks_truncated(model_text: str, errors: List[str]) -> bool:
    """
    An output cut off at the token cap ends mid-object: the last block fails to
    parse and the text does not close it. Distinguishable from ordinary junk.
    """
    if not any("unparseable block" in e for e in errors):
        return False
    tail = (model_text or "").rstrip()
    if not tail or tail.endswith(("}", "]")):
        return False
    # The tail has to be a JSON object that was cut off, not prose that never
    # was one: an opening brace with at least one quoted key after it.
    fragment = tail[tail.rfind("{") :] if "{" in tail else ""
    return bool(re.search(r'^\{\s*"[^"]+"\s*:', fragment))


def _describe_lost_object(model_text: str) -> str:
    """
    Name the line that truncation cost us. The fields that identify a product —
    index and name — come early in the object, so they usually survive the cut
    even when the object does not. Nothing here is written anywhere: a
    half-specified row is worse than a missing one, and the reviewer needs to
    know which line to chase.
    """
    tail = (model_text or "").rstrip()
    fragment = tail[tail.rfind("{") :] if "{" in tail else tail
    index = re.search(r'"index"\s*:\s*(\d+)', fragment)
    name = re.search(r'"name"\s*:\s*"([^"]{1,80})', fragment)
    if not index and not name:
        return ""
    which = f"line {index.group(1)}" if index else "a line"
    called = f" {name.group(1)!r}" if name else ""
    return f"{which}{called}"


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
        lost = _describe_lost_object(model_text)
        result.parse_errors.append(
            f"model output was truncated at the token cap and {lost or 'the last object'} was lost — "
            f"raise PRODUCT_EXTRACTION_MAX_TOKENS, or have the prompt carry large annexures by "
            f"reference instead of inline"
        )

    result.validation_warnings = _validate(result)
    return result
