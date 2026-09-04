from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator, AliasChoices, ConfigDict, BeforeValidator
from typing import Annotated
from datetime import datetime as dt_datetime


def _coerce_str_or_list(v: Any) -> List[str]:
    if isinstance(v, str):
        return [u.strip() for u in v.split(",") if u.strip()]
    if isinstance(v, list):
        out: List[str] = []
        for item in v:
            if isinstance(item, str):
                out.extend(u.strip() for u in item.split(",") if u.strip())
        return out
    return v


_StrOrList = Annotated[List[str], BeforeValidator(_coerce_str_or_list)]

def _clean_url(u: str) -> str:
    """
    Normalize attachment URLs coming from Glide / user text.
    - trims whitespace
    - strips surrounding quotes
    - replaces literal spaces with %20 (without touching already-encoded %20)
    - drops trailing punctuation that often appears in pasted strings
    """
    s = (u or "").strip()
    if not s:
        return ""
    # strip surrounding quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()

    # common trailing junk from copy-paste
    while s and s[-1] in (")", "]", "}", ","):
        s = s[:-1].rstrip()

    # keep fragments; but remove whitespace around them
    s = s.replace("\n", "").replace("\r", "").strip()

    # only replace literal spaces (Glide sometimes passes them)
    if " " in s:
        s = s.replace(" ", "%20")

    return s


class ProductItem(BaseModel):
    sr_no: Optional[int] = None
    name: str = Field(default="", alias="Name")
    qty: str = Field(default="", alias="Qty")
    details: str = Field(default="", alias="Details")
    dwg: Optional[str] = Field(default=None, alias="Dwg")
    photo: List[str] = Field(default_factory=list)
    files: List[str] = Field(default_factory=list)

    @property
    def all_attachment_urls(self) -> List[str]:
        urls: List[str] = []
        if self.dwg:
            urls.append(self.dwg)
        urls.extend(self.photo or [])
        urls.extend(self.files or [])

        # dedupe preserve order + clean
        seen = set()
        out: List[str] = []
        for u in urls:
            u2 = _clean_url(u or "")
            if u2 and u2 not in seen:
                seen.add(u2)
                out.append(u2)
        return out


def _normalize_product_obj(obj: Dict[str, Any]) -> Dict[str, Any]:
    # normalize common key variants
    if "Name" not in obj and "name" in obj:
        obj["Name"] = obj.get("name")
    if "Qty" not in obj and "qty" in obj:
        obj["Qty"] = obj.get("qty")
    if "Details" not in obj and "details" in obj:
        obj["Details"] = obj.get("details")
    if "Dwg" not in obj and "dwg" in obj:
        obj["Dwg"] = obj.get("dwg")
    return obj


def _parse_product_json_string(raw: str) -> List[Dict[str, Any]]:
    """
    Accept formats:
      1) single object JSON: {...}
      2) list JSON: [{...},{...}]
      3) broken "multi object" string (not valid JSON) like:
         {...}, {...}, {...}
         -> we wrap into [ ... ] safely.

    NOTE: We do best-effort repair; if still invalid, return [] (no crash).
    """
    s = (raw or "").strip()
    if not s:
        return []

    # Try strict JSON first
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return [_normalize_product_obj(parsed)]
        if isinstance(parsed, list):
            out: List[Dict[str, Any]] = []
            for it in parsed:
                if isinstance(it, dict):
                    out.append(_normalize_product_obj(it))
            return out
    except Exception:
        pass

    # Attempt repair for broken multi-object list
    repaired = s

    # common: "{...}, {...}, {...}" -> "[{...}, {...}, {...}]"
    # remove accidental trailing commas
    repaired = repaired.strip().rstrip(",")

    compact = repaired.replace("\n", " ").replace("\r", " ").strip()
    compact_nospace = compact.replace(" ", "")

    if compact.startswith("{") and compact.endswith("}") and "},{" in compact_nospace:
        repaired = "[" + compact + "]"
    elif compact.startswith("{") and "}, {" in compact:
        repaired = "[" + compact + "]"

    try:
        parsed2 = json.loads(repaired)
        if isinstance(parsed2, dict):
            return [_normalize_product_obj(parsed2)]
        if isinstance(parsed2, list):
            out2: List[Dict[str, Any]] = []
            for it in parsed2:
                if isinstance(it, dict):
                    out2.append(_normalize_product_obj(it))
            return out2
    except Exception:
        return []

    return []


class InputPayload(BaseModel):
    # Accept both rowID and row_id
    row_id: str = Field(default="", validation_alias=AliasChoices("rowID", "row_id"))

    title: str = Field(alias="Title")
    industry: str = Field(default="", alias="Industry")
    geography: str = Field(default="", alias="Geography")
    standard: str = Field(default="", alias="Standard")
    customer_name: str = Field(default="", alias="Customer name")

    product_json: str = Field(default="{}", alias="Product_json")

    extracted_attachment_text: str = Field(default="", alias="Extracted Attachment Text")

    # Multi-product
    products: List[ProductItem] = Field(default_factory=list)

    # Backward compat: first product shortcut
    product: Optional[ProductItem] = None

    @model_validator(mode="after")
    def parse_product_json(self) -> "InputPayload":
        raw = (self.product_json or "").strip()
        items = _parse_product_json_string(raw)

        self.products = [ProductItem.model_validate(it) for it in items] if items else []
        self.product = self.products[0] if self.products else None
        return self

    def all_attachment_urls(self) -> List[str]:
        urls: List[str] = []
        for p in self.products:
            urls.extend(p.all_attachment_urls)

        # dedupe preserve order
        seen = set()
        out: List[str] = []
        for u in urls:
            u2 = _clean_url(u or "")
            if u2 and u2 not in seen:
                seen.add(u2)
                out.append(u2)
        return out


class WebFinding(BaseModel):
    title: str
    url: str
    snippet: str = ""


class AttachmentFinding(BaseModel):
    url: str
    kind: str
    summary: str
    data: Dict[str, Any] = Field(default_factory=dict)


class OutputPayload(BaseModel):
    run_id: str
    mode: str  # "pricing" | "summary"
    row_id: str

    rfq_title: str
    customer_name: str = ""
    standard: str = ""
    geography: str = ""
    industry: str = ""

    product_name: str = ""
    product_qty: str = ""
    product_details: str = ""

    attachment_findings: List[AttachmentFinding] = Field(default_factory=list)
    web_findings: List[WebFinding] = Field(default_factory=list)

    pricing_estimate_text: str = ""
    pricing_reasoning_text: str = ""

    summary_text: str = ""
    scope_text: str = ""
    cost_text: str = ""
    quality_text: str = ""
    timeline_text: str = ""

    raw_model_output: str = ""
    # ---- instrumentation ----
    timings: Dict[str, Any] = Field(default_factory=dict)
    docai: Dict[str, Any] = Field(default_factory=dict)
    structured: Dict[str, Any] = Field(default_factory=dict)

class QueryPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    row_id: str = Field(default="", validation_alias=AliasChoices("rowID", "row_id"))
    subject: str = Field(default="")
    from_: str = Field(default="", alias="from_")
    from_name: str = Field(default="")
    body: str = Field(default="")
    received_at: str = Field(default="")
    requested_by: str = Field(
        default="",
        validation_alias=AliasChoices("requested_by", "requestedBy", "Created By", "Created_By", "created_by"),
    )
    requested_time: str = Field(
        default="",
        validation_alias=AliasChoices("requested_time", "requestedTime", "Created at", "Created_At", "created_at"),
    )
    attachment_urls: _StrOrList = Field(
        default_factory=list,
        validation_alias=AliasChoices("attachment_urls", "attached_urls")
    )
    attached_media: _StrOrList = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_glide_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        if "requested_by" not in data:
            for key in ("requestedBy", "Created By", "Created_By", "created_by"):
                if key in data:
                    data["requested_by"] = data.get(key)
                    break
        if "requested_time" not in data:
            for key in ("requestedTime", "Created at", "Created_At", "created_at"):
                if key in data:
                    data["requested_time"] = data.get(key)
                    break
        if "attachment_urls" not in data and "attached_urls" in data:
            data["attachment_urls"] = data.get("attached_urls")

        # Unwrap single-element lists for string fields
        for field in ("subject", "from_name", "body", "received_at", "from_", "requested_by", "requested_time"):
            val = data.get(field)
            if isinstance(val, list):
                data[field] = val[0] if val else ""

        # Normalize received_at to "YYYY-MM-DD HH:MM:SS"
        raw_ts = data.get("received_at", "")
        if isinstance(raw_ts, str) and raw_ts.strip():
            try:
                parsed = dt_datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                data["received_at"] = parsed.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        raw_requested_ts = data.get("requested_time", "")
        if isinstance(raw_requested_ts, str) and raw_requested_ts.strip():
            try:
                parsed = dt_datetime.fromisoformat(raw_requested_ts.replace("Z", "+00:00"))
                data["requested_time"] = parsed.isoformat()
            except ValueError:
                pass

        return data
    
    def all_attachment_urls(self) -> List[str]:
        seen = set()
        out: List[str] = []
        for u in self.attachment_urls:
            u2 = _clean_url(u or "")
            if u2 and u2 not in seen:
                seen.add(u2)
                out.append(u2)
        return out

def _coerce_optional_str(v: Any) -> Optional[str]:
    """Model output is free-form: numbers, bools and lists all become clean strings."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        parts = [x for x in (_coerce_optional_str(i) for i in v) if x]
        return ", ".join(parts) or None
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _coerce_str(v: Any) -> str:
    return _coerce_optional_str(v) or ""


_OptStr = Annotated[Optional[str], BeforeValidator(_coerce_optional_str)]
_LooseStr = Annotated[str, BeforeValidator(_coerce_str)]


QUANTITY_BASES = {
    "annual", "one_time", "blanket", "price_breaks", "release_schedule", "not_stated",
}
PROVENANCE_TOKENS = {"verbatim", "derived", "internal", "not_stated", "unknown"}
# No "commercial": currency, incoterm and payment terms are held in our own
# systems, so they are never asked and never raised as a gap.
QUERY_SECTIONS = {
    "specification", "scope", "application", "standards",
    "additional_note", "quantity",
}
PLACEHOLDER = "\\--"


class ProductAnnexure(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    required: bool = False
    by_reference: bool = False
    suggested_filename: _LooseStr = ""
    columns: List[str] = Field(default_factory=list)
    rows: List[Any] = Field(default_factory=list)


class ExtractedQuery(BaseModel):
    """One row of the queries table: a single question for the customer."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    query_ref: _LooseStr = ""
    # The product indexes this question covers. One question that applies to
    # several lines is one row, not one per line. Empty = blocks every line.
    product_refs: List[int] = Field(default_factory=list)
    section: _LooseStr = ""
    description: _LooseStr = ""
    photo: _StrOrList = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)

        # Tolerate the v2 wording and the Glide column labels.
        for src, dst in (
            ("text", "description"),
            ("Query Description", "description"),
            ("blocks", "product_ref"),
            ("field", "section"),
            ("Query Photo", "photo"),
        ):
            if src in data and dst not in data:
                data[dst] = data.get(src)

        # Accept 1, [1, 4], "1,4", "line 1" or null — all become a list of ints.
        ref = data.pop("product_ref", None) if "product_refs" not in data else data.get("product_refs")
        refs: List[int] = []
        if isinstance(ref, (int, float)) and not isinstance(ref, bool):
            refs = [int(ref)]
        elif isinstance(ref, str):
            refs = [int(part) for part in re.findall(r"\d+", ref)]
        elif isinstance(ref, list):
            for item in ref:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    refs.append(int(item))
                elif isinstance(item, str):
                    refs.extend(int(part) for part in re.findall(r"\d+", item))
        # Preserve order, drop repeats.
        data["product_refs"] = list(dict.fromkeys(refs))

        if data.get("photo") is None:
            data["photo"] = []

        # A response is the customer's to give; never accept one from the model.
        data.pop("response", None)
        data.pop("Query Response", None)
        return data

    def is_rfq_level(self) -> bool:
        return not self.product_refs

    def is_emittable(self) -> bool:
        return bool((self.description or "").strip())

    def photo_text(self) -> str:
        return ", ".join([u for u in (self.photo or []) if (u or "").strip()])


class ExtractedProduct(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    index: Optional[int] = None
    source_ref: _LooseStr = ""
    name: _LooseStr = ""
    structure: _LooseStr = "single"
    variant_count: Optional[int] = None
    quantity: _LooseStr = ""
    quantity_basis: _LooseStr = "not_stated"
    details: _LooseStr = ""
    internal_notes: _LooseStr = ""
    target_price: _OptStr = None
    dwg_link: _OptStr = None
    rep_url: _OptStr = None
    addl_files: _StrOrList = Field(default_factory=list)
    annexure: Optional[ProductAnnexure] = None
    provenance: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        data = dict(data)

        # The model sometimes emits Glide column labels instead of the NDJSON keys.
        aliases = {
            "Product name": "name",
            "Name": "name",
            "Qty": "quantity",
            "RFQ Details": "details",
            "Details": "details",
            "AI Internal notes": "internal_notes",
            "Target price": "target_price",
            "target price": "target_price",
            "Dwg link": "dwg_link",
            "Rep URL": "rep_url",
            "rep_URL": "rep_url",
            "Addl. files": "addl_files",
            "addl_file": "addl_files",
        }
        for src, dst in aliases.items():
            if src in data and dst not in data:
                data[dst] = data.get(src)

        # v2 emitted {"value": ..., "basis": ...}; v3 wants one string.
        qty = data.get("quantity")
        if isinstance(qty, dict):
            value = _coerce_optional_str(qty.get("value")) or ""
            basis = _coerce_optional_str(qty.get("basis")) or ""
            data["quantity"] = f"{value} ({basis})" if value and basis else (value or basis)

        basis_token = _coerce_optional_str(data.get("quantity_basis")) or "not_stated"
        basis_token = basis_token.strip().lower().replace("-", "_").replace(" ", "_")
        data["quantity_basis"] = basis_token if basis_token in QUANTITY_BASES else "not_stated"

        for key in ("index", "variant_count"):
            val = data.get(key)
            if isinstance(val, str):
                digits = val.strip()
                data[key] = int(digits) if digits.isdigit() else None

        prov = data.get("provenance")
        if isinstance(prov, dict):
            data["provenance"] = {
                str(k): (_coerce_optional_str(v) or "") for k, v in prov.items()
            }
        elif prov is not None:
            data["provenance"] = {}

        annexure = data.get("annexure")
        if isinstance(annexure, bool):
            data["annexure"] = {"required": annexure} if annexure else None

        if data.get("addl_files") is None:
            data["addl_files"] = []

        # Queries and assumptions have their own homes now; never carried on a product.
        data.pop("queries", None)
        data.pop("assumptions", None)
        return data

    def is_emittable(self) -> bool:
        """A row with no name is not quotable and must never reach the product table."""
        return bool((self.name or "").strip())

    def placeholder_count(self) -> int:
        return (self.details or "").count(PLACEHOLDER)

    def addl_files_text(self) -> str:
        return ", ".join([u for u in (self.addl_files or []) if (u or "").strip()])

    def bad_provenance_tokens(self) -> List[str]:
        """Provenance must be one token per field — a phrase means the model improvised."""
        return [
            f"{k}={v}"
            for k, v in (self.provenance or {}).items()
            if (v or "").strip() and (v or "").strip().lower() not in PROVENANCE_TOKENS
        ]


class ProductExtractionHeader(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    project: _LooseStr = ""
    rfq_title: _LooseStr = ""
    line_count_expected: Optional[int] = None
    line_count_extracted: Optional[int] = None
    reconciliation: _LooseStr = ""
    common_conditions: _LooseStr = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        # v2 named this "customer"; v3 anonymises it to a project name.
        if "project" not in data and "customer" in data:
            data["project"] = data.get("customer")
        for key in ("line_count_expected", "line_count_extracted"):
            val = data.get(key)
            if isinstance(val, str):
                digits = val.strip()
                data[key] = int(digits) if digits.isdigit() else None
            elif isinstance(val, float):
                data[key] = int(val)
        return data


class ProductExtractionSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    placeholder_count: Optional[int] = None
    query_count: Optional[int] = None
    notes_for_reviewer: _LooseStr = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for key in ("placeholder_count", "query_count"):
            val = data.get(key)
            if isinstance(val, str):
                digits = val.strip()
                data[key] = int(digits) if digits.isdigit() else None
            elif isinstance(val, float):
                data[key] = int(val)
        return data


class ProductExtractionResult(BaseModel):
    """Parsed NDJSON from the product-extraction prompt."""

    header: Optional[ProductExtractionHeader] = None
    products: List[ExtractedProduct] = Field(default_factory=list)
    queries: List[ExtractedQuery] = Field(default_factory=list)
    summary: Optional[ProductExtractionSummary] = None
    skipped_products: List[Dict[str, Any]] = Field(default_factory=list)
    parse_errors: List[str] = Field(default_factory=list)
    # Prompt rules the model broke — the appendix asks for these in code, not prose.
    validation_warnings: List[str] = Field(default_factory=list)
    raw_model_output: str = ""

    def reconciliation_note(self) -> str:
        """Count mismatch the reviewer needs to see, or empty when counts line up."""
        expected = self.header.line_count_expected if self.header else None
        if expected is None:
            return ""
        if expected == len(self.products):
            return ""
        return f"line_count_expected={expected} but {len(self.products)} product line(s) parsed"

    def queries_for(self, product_index: Optional[int]) -> List[ExtractedQuery]:
        return [q for q in self.queries if product_index in q.product_refs]

    def rfq_level_queries(self) -> List[ExtractedQuery]:
        return [q for q in self.queries if q.is_rfq_level()]


class TriageOutputPayload(BaseModel):
    run_id: str
    mode: str = "triage"
    row_id: str
    triage_text: str = ""
    costing_estimate_text: str = ""
    costing_estimate_reason_text: str = ""
    raw_model_output: str = ""
    raw_costing_model_output: str = ""
    raw_products_model_output: str = ""

    product_extraction: Optional[ProductExtractionResult] = None
    # In-flight product-extraction handle, resolved after the triage output has
    # been written. Never serialised.
    pending_products: Any = Field(default=None, exclude=True, repr=False)

    attachment_findings: List[AttachmentFinding] = Field(default_factory=list)

    timings: Dict[str, Any] = Field(default_factory=dict)
    docai: Dict[str, Any] = Field(default_factory=dict)
    structured: Dict[str, Any] = Field(default_factory=dict)


class RfqClassificationInputPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    row_id: str = Field(default="", validation_alias=AliasChoices("rowID", "row_id"))
    mail_body: str = Field(default="", validation_alias=AliasChoices("mail_body", "body", "Name"))
    subject: str = Field(default="", validation_alias=AliasChoices("subject", "subject_line", "9lbwR"))
    from_: str = Field(default="", validation_alias=AliasChoices("from_", "from", "vt1tN"))
    from_name: str = Field(default="", validation_alias=AliasChoices("from_name", "sflMP"))

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for field in ("rowID", "row_id", "mail_body", "body", "Name", "subject", "subject_line", "9lbwR", "from_", "from", "vt1tN", "from_name", "sflMP"):
            val = data.get(field)
            if isinstance(val, list):
                data[field] = val[0] if val else ""
        return data


class RfqClassificationOutputPayload(BaseModel):
    run_id: str
    mode: str = "classify"
    row_id: str
    geography: str = ""
    industry: str = ""
    client_name: str = ""
    standards: str = ""
    title: str = ""
    sequence: str = ""
    raw_client_name: str = ""
    raw_model_output: str = ""
    structured: Dict[str, Any] = Field(default_factory=dict)


class RfqRegenerateTriageInputPayload(BaseModel):
    rfq_id: str = ""
    instruction: str = ""
    previous_instructions: Any = Field(default_factory=list)
    rfq: Dict[str, Any] = Field(default_factory=dict)
    products: List[Dict[str, Any]] = Field(default_factory=list)
    google_attachment_ids: List[str] = Field(default_factory=list)
    requested_time: str = ""
    requested_by: str = ""
    version: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "rfq_id" not in data and "rfqId" in data:
            data["rfq_id"] = data.get("rfqId")
        if "version" not in data and "Version" in data:
            data["version"] = data.get("Version")
        if data.get("version") is not None:
            data["version"] = str(data.get("version"))
        if "previous_instructions" not in data and "previousInstructions" in data:
            data["previous_instructions"] = data.get("previousInstructions")

        prev = data.get("previous_instructions")
        if isinstance(prev, str) and prev.strip():
            try:
                data["previous_instructions"] = json.loads(prev)
            except json.JSONDecodeError:
                data["previous_instructions"] = prev

        val = data.get("google_attachment_ids")
        if isinstance(val, str):
            data["google_attachment_ids"] = [u.strip() for u in val.split(",") if u.strip()]
        elif isinstance(val, list):
            data["google_attachment_ids"] = [str(u).strip() for u in val if str(u).strip()]

        if isinstance(data.get("products"), dict):
            data["products"] = [data["products"]]
        return data


class RfqRegenerateTriageOutputPayload(BaseModel):
    run_id: str
    mode: str = "regenerate_triage"
    rfq_id: str
    instruction: str = ""
    triage_text: str = ""
    costing_estimate_text: str = ""
    costing_estimate_reason_text: str = ""
    raw_model_output: str = ""
    raw_costing_model_output: str = ""
    attachment_findings: List[AttachmentFinding] = Field(default_factory=list)
    timings: Dict[str, Any] = Field(default_factory=dict)
    structured: Dict[str, Any] = Field(default_factory=dict)


class RfqQueryInputPayload(RfqRegenerateTriageInputPayload):
    query: str = ""


class RfqQueryOutputPayload(BaseModel):
    run_id: str
    mode: str = "query_regenerate"
    rfq_id: str
    query: str = ""
    response_text: str = ""
    raw_model_output: str = ""
    attachment_findings: List[AttachmentFinding] = Field(default_factory=list)
    timings: Dict[str, Any] = Field(default_factory=dict)
    structured: Dict[str, Any] = Field(default_factory=dict)
