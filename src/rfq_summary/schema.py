from __future__ import annotations

import json
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

class TriageOutputPayload(BaseModel):
    run_id: str
    mode: str = "triage"
    row_id: str
    triage_text: str = ""
    costing_estimate_text: str = ""
    costing_estimate_reason_text: str = ""
    raw_model_output: str = ""
    raw_costing_model_output: str = ""

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
    raw_model_output: str = ""
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
