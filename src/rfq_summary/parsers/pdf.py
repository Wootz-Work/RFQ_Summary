from __future__ import annotations

import base64
import re
from typing import Any, Dict, List

import fitz  # type: ignore
from ..config import Settings
from ..schema import AttachmentFinding
import json

from google.oauth2.service_account import Credentials
from google.api_core.client_options import ClientOptions
from google.cloud import documentai_v1 as documentai

def _clean_text(s: str) -> str:
    s = (s or "").replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _docai_enabled(settings: Settings) -> bool:
    if not getattr(settings, "enable_docai_ocr", False):
        return False
    if not (getattr(settings, "docai_project_id", "") or "").strip():
        return False
    if not (getattr(settings, "docai_location", "") or "").strip():
        return False
    if not (getattr(settings, "docai_processor_id", "") or "").strip():
        return False
    sa = (getattr(settings, "docai_sa_json_b64", "") or "").strip() or (getattr(settings, "google_sa_json_b64", "") or "").strip()
    return bool(sa)


def _docai_client(settings: Settings) -> documentai.DocumentProcessorServiceClient:
    sa_b64 = (getattr(settings, "docai_sa_json_b64", "") or "").strip() or (getattr(settings, "google_sa_json_b64", "") or "").strip()
    if not sa_b64:
        raise RuntimeError("Missing DOCAI_SA_JSON_B64 (or GOOGLE_SA_JSON_B64 fallback) for Document AI OCR.")

    info = json.loads(base64.b64decode(sa_b64).decode("utf-8"))
    creds = Credentials.from_service_account_info(info)

    loc = (settings.docai_location or "").strip()
    endpoint = f"{loc}-documentai.googleapis.com"

    return documentai.DocumentProcessorServiceClient(
        credentials=creds,
        client_options=ClientOptions(api_endpoint=endpoint),
    )


def _docai_ocr_pdf(settings: Settings, pdf_bytes: bytes) -> List[str]:
    """
    Run Document AI OCR processor on the PDF.
    Returns per-page text (best effort). If anchors are weak, returns whole-doc text as one element.
    """
    client = _docai_client(settings)
    name = client.processor_path(settings.docai_project_id, settings.docai_location, settings.docai_processor_id)

    raw_document = documentai.RawDocument(content=pdf_bytes, mime_type="application/pdf")
    req = documentai.ProcessRequest(name=name, raw_document=raw_document)

    timeout = int(getattr(settings, "docai_timeout_sec", 120))
    result = client.process_document(request=req, timeout=timeout)
    doc = result.document

    full_text = (doc.text or "")
    if not full_text.strip():
        return []

    def anchor_text(anchor) -> str:
        parts: List[str] = []
        for seg in getattr(anchor, "text_segments", []) or []:
            s = int(getattr(seg, "start_index", 0) or 0)
            e = int(getattr(seg, "end_index", 0) or 0)
            if e > s:
                parts.append(full_text[s:e])
        return "".join(parts).strip()

    page_texts: List[str] = []
    pages = getattr(doc, "pages", []) or []
    for p in pages:
        txt = ""
        try:
            if getattr(p, "layout", None) and getattr(p.layout, "text_anchor", None):
                txt = anchor_text(p.layout.text_anchor)
        except Exception:
            txt = ""
        page_texts.append(_clean_text(txt))

    # If per-page anchors are too empty, fallback to whole doc text.
    if sum(len(t) for t in page_texts) < 80:
        return [_clean_text(full_text)]

    return page_texts


def _build_pdf_extracted_text(
    settings: Settings,
    n_pages: int,
    page_texts: List[str],
    page_ocr_samples: List[Dict[str, Any]],
    page_vision_samples: List[Dict[str, Any]],
    scanned_like: bool,
) -> str:
    """
    Build a bounded, page-aware extracted_text blob for LLM consumption.

    Goals:
      - include real content (not just metadata)
      - keep page labels so the model can cite "page X"
      - bound total chars to avoid context blow-ups

    NOTE: No new env var. Internal cap.
    """
    MAX_PDF_TEXT_CHARS = 140_000
    remaining = MAX_PDF_TEXT_CHARS
    blocks: List[str] = []

    blocks.append(f"## PDF SUMMARY: pages={n_pages} mode={'scanned' if scanned_like else 'text'}")
    remaining -= len(blocks[-1]) + 2

    def add_block(s: str) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        s2 = (s or "").strip()
        if not s2:
            return
        if len(s2) + 2 > remaining:
            # add truncated marker once
            blocks.append("...[TRUNCATED: pdf extracted_text exceeded budget]...")
            remaining = 0
            return
        blocks.append(s2)
        remaining -= len(s2) + 2

    if not scanned_like:
        # Prefer selectable text per page (bounded per page)
        # Keep many pages but limit per-page characters.
        per_page_cap = 2200
        for i in range(n_pages):
            txt = (page_texts[i] or "").strip()
            if not txt:
                continue
            add_block(f"### PAGE {i+1} [TEXT]\n{txt[:per_page_cap]}")
            if remaining <= 0:
                break
        return "\n\n".join(blocks).strip()

    # scanned_like: combine OCR + vision
    # OCR tends to be more "verbatim"; vision is "semantic".
    # We include both, but cap each page chunk.
    ocr_map: Dict[int, str] = {}
    for d in page_ocr_samples:
        try:
            p = int(d.get("page", 0))
            ocr_map[p] = (d.get("text") or "").strip()
        except Exception:
            continue

    vis_map: Dict[int, str] = {}
    for d in page_vision_samples:
        try:
            p = int(d.get("page", 0))
            vis_map[p] = (d.get("text") or "").strip()
        except Exception:
            continue

    for p in range(1, n_pages + 1):
        ocr = (ocr_map.get(p) or "").strip()
        vis = (vis_map.get(p) or "").strip()

        if not ocr and not vis:
            continue

        page_lines: List[str] = [f"### PAGE {p} [OCR+VISION]"]
        if ocr:
            page_lines.append("OCR:")
            page_lines.append(ocr[:1800])
        if vis:
            page_lines.append("VISION:")
            page_lines.append(vis[:1800])

        add_block("\n".join(page_lines))
        if remaining <= 0:
            break

    return "\n\n".join(blocks).strip()


def analyze_pdf_bytes(settings: Settings, url: str, data: bytes) -> AttachmentFinding:
    max_pages = int(settings.max_pdf_pages)
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        n_pages = min(len(doc), max_pages)

        page_text_samples: List[Dict[str, Any]] = []
        page_ocr_samples: List[Dict[str, Any]] = []
        page_vision_samples: List[Dict[str, Any]] = []

        total_text_chars = 0
        page_texts: List[str] = []

        # 1) selectable text
        for i in range(n_pages):
            page = doc.load_page(i)
            txt = _clean_text(page.get_text("text") or "")
            page_texts.append(txt)
            total_text_chars += len(txt)

        avg_text = total_text_chars / max(1, n_pages)
        scanned_like = avg_text < settings.min_pdf_text_chars_per_page

        docai_used = False
        docai_error = ""
        docai_pages_returned = 0
        docai_pages_used = 0
        # If DocAI is enabled but not configured, record it clearly (no silent skip).
        docai_configured = _docai_enabled(settings)
        if scanned_like and getattr(settings, "enable_docai_ocr", False) and not docai_configured:
            docai_error = "DocAI enabled but not configured (missing project/location/processor/SA JSON)."
        # If scanned-like, try DocAI OCR (retry once). No local OCR fallback.
        if scanned_like and _docai_enabled(settings):
            for attempt in (1, 2):
                try:
                    docai_pages = _docai_ocr_pdf(settings, data)
                    if docai_pages:
                        docai_pages_returned = len(docai_pages)
                        page_texts = docai_pages[:n_pages] if len(docai_pages) > 1 else docai_pages
                        docai_pages_used = len(page_texts)
                        scanned_like = False
                        docai_used = True
                        break
                except Exception as e:
                    docai_error = f"{type(e).__name__}: {e}"
                    docai_used = False
        # If still no text, move ahead but log in extracted text
        if scanned_like and not docai_used:
            note = "DocAI OCR failed (or not configured) for scanned PDF. No text extracted."
            if docai_error:
                note += f" Last error: {docai_error}"
            page_texts = [note]
            scanned_like = False

        for idx in range(min(n_pages, 5)):
            if idx < len(page_texts) and page_texts[idx]:
                page_text_samples.append({"page": idx + 1, "text": page_texts[idx][:1400]})

        extracted_text = _build_pdf_extracted_text(
            settings=settings,
            n_pages=n_pages,
            page_texts=page_texts,
            page_ocr_samples=page_ocr_samples,
            page_vision_samples=page_vision_samples,
            scanned_like=scanned_like,
        )

        excerpt = "\n".join([t[:700] for t in page_texts if t][:3]).strip()
        summary = (
            f"PDF analyzed ({n_pages} page(s)). Text extracted. docai_used={docai_used}.\n"
            f"Top excerpts:\n{excerpt if excerpt else '(no excerpt)'}"
        )

        data_out = {
            "pages": n_pages,
            "mode": ("text+docai" if docai_used else "text"),
            "page_text_samples": page_text_samples,
            "extracted_text": extracted_text,

            # ---- docai instrumentation ----
            "docai_used": bool(docai_used),
            "docai_pages_used": int(docai_pages_used if docai_used else 0),
            "docai_pages_returned": int(docai_pages_returned if docai_used else 0),
            "docai_error": (docai_error or ""),
        }

        return AttachmentFinding(
            url=url,
            kind="pdf",
            summary=summary.strip(),
            data=data_out,
        )

    finally:
        try:
            doc.close()
        except Exception:
            pass