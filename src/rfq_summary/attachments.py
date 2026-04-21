from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from typing import List, Tuple, Optional
from urllib.parse import urlparse

import httpx

from .config import Settings
from .schema import AttachmentFinding
from .parsers.pdf import analyze_pdf_bytes
from .parsers.excel import analyze_excel_bytes
from .parsers.image import analyze_image_bytes
from .gdrive import fetch_drive_file

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Google Drive file IDs are 25–44 url-safe base64 chars with no dots or slashes
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{25,44}$")


def _is_google_drive_id(value: str) -> bool:
    """Return True if the string looks like a bare Google Drive file ID."""
    return bool(_DRIVE_ID_RE.fullmatch(value.strip()))


def _clean_url(u: str) -> str:
    s = (u or "").strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    s = s.replace("\n", "").replace("\r", "").strip()
    while s and s[-1] in (")", "]", "}", ","):
        s = s[:-1].rstrip()
    if " " in s:
        s = s.replace(" ", "%20")
    return s


def _is_probably_ms_folder_link(url: str) -> bool:
    u = (url or "").lower()
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
    except Exception:
        host = ""
    if ":f:" in u:
        return True
    is_ms_host = (
        host == "sharepoint.com"
        or host.endswith(".sharepoint.com")
        or host == "onedrive.live.com"
        or host.endswith(".onedrive.live.com")
    )
    return is_ms_host and ("?e=" in u or "cid=" in u) and ("folder" in u)


def _guess_kind(url: str, content_type: str | None) -> str:
    u = (url or "").lower()
    ct = (content_type or "").lower()

    if _is_probably_ms_folder_link(u):
        return "folder"
    if u.endswith(".pdf") or ct.startswith("application/pdf"):
        return "pdf"
    if u.endswith(".xlsx") or u.endswith(".xlsm") or "spreadsheet" in ct:
        return "excel"
    if any(u.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]) or ct.startswith("image/"):
        return "image"
    return "unknown"


def _safe_filename_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        name = (p.path.rsplit("/", 1)[-1] or "file").strip()
        return name[:120] or "file"
    except Exception:
        return "file"


# ---------------------------------------------------------------------------
# HTTP fetcher (unchanged — still used for plain URLs)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HttpFetcher:
    settings: Settings

    def fetch(self, url: str) -> Tuple[bytes, Optional[str]]:
        max_bytes = int(self.settings.max_attachment_bytes)
        logger.debug("[HTTP] Fetching URL: %s", url)

        headers = {"User-Agent": "rfq-summary-bot/1.0", "Accept": "*/*"}
        timeout = httpx.Timeout(connect=15.0, read=45.0, write=15.0, pool=15.0)

        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            content_type = None

            # HEAD best effort (some hosts block HEAD; ignore failures)
            try:
                h = client.head(url)
                if h.status_code < 400:
                    content_type = h.headers.get("content-type")
                    cl = h.headers.get("content-length")
                    logger.debug("[HTTP] HEAD OK — content-type=%s content-length=%s", content_type, cl)
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        raise ValueError(f"Attachment too large (content-length={cl} > {max_bytes})")
            except Exception:
                logger.debug("[HTTP] HEAD failed or skipped for %s", url)

            r = client.get(url)
            r.raise_for_status()

            content_type = r.headers.get("content-type") or content_type
            data = r.content
            logger.debug("[HTTP] GET OK — bytes=%d content-type=%s", len(data), content_type)

            if len(data) > max_bytes:
                raise ValueError(f"Attachment too large (bytes={len(data)} > {max_bytes})")

        return data, content_type


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _parse_attachment_input(raw: str) -> List[str]:
    """
    Glide sends attachments as a comma-separated string of URLs or Drive file IDs.
    Returns a cleaned list of individual values.
    """
    return [u.strip() for u in raw.split(",") if u.strip()]


def _dispatch_finding(settings: Settings, u: str, data: bytes, ctype: Optional[str]) -> AttachmentFinding:
    """Route bytes to the correct parser based on kind."""
    kind = _guess_kind(u, ctype)
    fname = _safe_filename_from_url(u) if u.startswith("http") else f"drive_{u}"
    logger.debug("[DISPATCH] ref=%s kind=%s bytes=%d content-type=%s", u, kind, len(data), ctype)

    if kind == "pdf":
        logger.debug("[DISPATCH] Routing to analyze_pdf_bytes")
        return analyze_pdf_bytes(settings, u, data)
    elif kind == "excel":
        logger.debug("[DISPATCH] Routing to analyze_excel_bytes")
        return analyze_excel_bytes(settings, u, data)
    elif kind == "image":
        logger.debug("[DISPATCH] Routing to analyze_image_bytes")
        return analyze_image_bytes(settings, u, data)
    else:
        logger.debug("[DISPATCH] Unsupported type — returning kind=unknown")
        mt, _ = mimetypes.guess_type(u)
        return AttachmentFinding(
            url=u,
            kind="unknown",
            summary=f"Downloaded '{fname}'. Unsupported type (content-type={ctype or mt or 'unknown'}).",
            data={"filename": fname, "content_type": ctype or mt or ""},
        )


def analyze_attachments(settings: Settings, urls: List[str]) -> List[AttachmentFinding]:
    """
    Accepts a list of:
      - Plain URLs (mailparser, etc.)         → fetched via HttpFetcher
      - Bare Google Drive file IDs            → fetched via service account
      - Comma-separated strings of the above  → split automatically
    """
    out: List[AttachmentFinding] = []
    fetcher = HttpFetcher(settings)

    # Flatten: handle cases where a single list entry is itself comma-separated
    expanded: List[str] = []
    for entry in urls:
        expanded.extend(_parse_attachment_input(entry))

    logger.debug("[ATTACHMENTS] Total entries after expand: %d", len(expanded))

    for url in expanded:
        u = _clean_url(url)
        if not u:
            logger.debug("[ATTACHMENTS] Skipping empty entry")
            continue

        logger.debug("[ATTACHMENTS] Processing: %s", u)

        # ----------------------------------------------------------------
        # Branch 1: Bare Google Drive file ID (from Glide via Power Automate)
        # ----------------------------------------------------------------
        if _is_google_drive_id(u):
            logger.debug("[ATTACHMENTS] Detected as Google Drive file ID")
            try:
                data, ctype = fetch_drive_file(u, settings.google_service_account_path)
                logger.debug("[ATTACHMENTS] Drive fetch OK — bytes=%d ctype=%s", len(data), ctype)
                finding = _dispatch_finding(settings, u, data, ctype)
                logger.debug("[ATTACHMENTS] Drive parse OK — kind=%s", finding.kind)
            except Exception as e:
                logger.error("[ATTACHMENTS] Drive fetch/parse failed for %s: %s: %s", u, type(e).__name__, e)
                finding = AttachmentFinding(
                    url=u,
                    kind="unknown",
                    summary=f"Failed to fetch Drive file '{u}': {type(e).__name__}: {e}",
                    data={"filename": f"drive_{u}"},
                )
            out.append(finding)
            continue

        # ----------------------------------------------------------------
        # Branch 2: MS SharePoint/OneDrive folder link
        # ----------------------------------------------------------------
        if _is_probably_ms_folder_link(u):
            logger.debug("[ATTACHMENTS] Detected as MS folder link — skipping deep traversal")
            out.append(
                AttachmentFinding(
                    url=u,
                    kind="folder",
                    summary=(
                        "Folder link detected (SharePoint/OneDrive). "
                        "Deep traversal requires Microsoft Graph integration."
                    ),
                    data={"filename": _safe_filename_from_url(u), "action": "graph_required"},
                )
            )
            continue

        # ----------------------------------------------------------------
        # Branch 3: Plain HTTP/HTTPS URL (mailparser or other sources)
        # ----------------------------------------------------------------
        logger.debug("[ATTACHMENTS] Treating as plain HTTP URL")
        try:
            data, ctype = fetcher.fetch(u)
            finding = _dispatch_finding(settings, u, data, ctype)
            logger.debug("[ATTACHMENTS] HTTP fetch/parse OK — kind=%s", finding.kind)
            out.append(finding)

        except Exception as e:
            logger.error("[ATTACHMENTS] HTTP fetch/parse failed for %s: %s: %s", u, type(e).__name__, e)
            out.append(
                AttachmentFinding(
                    url=u,
                    kind="unknown",
                    summary=f"Failed to analyze attachment: {type(e).__name__}: {e}",
                    data={"filename": _safe_filename_from_url(u)},
                )
            )

    logger.debug("[ATTACHMENTS] Done — %d findings returned", len(out))
    return out