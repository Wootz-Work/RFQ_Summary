"""
Uploading a generated annexure to OneDrive / SharePoint, through the same
Microsoft Graph app registration that sends the notification mail.

No new auth: `_get_app_token` requests the `.default` scope, so a token
carries whatever application permissions are consented on that registration.
Adding file upload is a permission grant in Entra ID — `Sites.Selected`
scoped to the one library, or the much broader `Files.ReadWrite.All` — and
no code change to the token path at all.

Addressing: a DriveItem id (`01DDVW3I…`) is only meaningful inside a drive,
because Graph has no global `/driveItems/{id}` endpoint. Both halves come
off the RFQ row — the drive as well as the folder — so one RFQ can live in
a different library from another without a redeploy. MS_GRAPH_DRIVE_ID is
a fallback for a row that carries a folder but no drive.

Nothing here may fail a run. An upload that does not happen costs a link in
a Glide cell; an exception escaping this module costs the extraction.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote

import httpx

from .config import Settings
from .emailer import _get_app_token

GRAPH = "https://graph.microsoft.com/v1.0"

# `01ABCDEF…` — the form Graph actually addresses. A SharePoint UniqueId
# (a bare GUID) names the same folder but cannot be used as a path segment,
# so it is worth telling the two apart in a log rather than 404-ing.
_ITEM_ID = re.compile(r"^[A-Za-z0-9]{20,}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Graph's simple upload tops out at 4 MB; past that it needs an upload
# session. A 5,000-row annexure is ~275 KB, so the session path is not worth
# writing until something actually approaches the limit.
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024


def upload_configured(settings: Settings) -> bool:
    """Graph credentials only: the drive and folder arrive per RFQ, not here."""
    return bool(
        settings.enable_annexure_upload
        and (settings.ms_graph_tenant_id or "").strip()
        and (settings.ms_graph_client_id or "").strip()
        and (settings.ms_graph_client_secret or "").strip()
    )


def upload_annexure(
    settings: Settings,
    drive_id: str,
    folder_id: str,
    filename: str,
    data: bytes,
) -> Optional[str]:
    """
    Put one annexure in the RFQ's folder and return the link to it.

    Returns None whenever the file did not land — not configured, no folder
    on the RFQ row, too large, or Graph refused. Every one of those is a
    normal outcome the caller reports; none is an exception.

    Conflicts rename rather than replace. A team member editing the sheet in
    place is the whole point of putting it on OneDrive, and a regeneration
    silently overwriting their work is the one failure that would stop them
    trusting it. Clutter is recoverable; their afternoon is not.
    """
    if not upload_configured(settings):
        return None

    drive_id = (drive_id or "").strip() or (settings.ms_graph_drive_id or "").strip()
    if not drive_id:
        print("[WARN] annexure | no drive id on the RFQ row and no fallback configured — not uploaded")
        return None

    folder_id = (folder_id or "").strip()
    if not folder_id:
        print("[WARN] annexure | no folder id on the RFQ row — not uploaded")
        return None
    if _GUID.match(folder_id):
        print(
            f"[WARN] annexure | folder id {folder_id!r} is a SharePoint UniqueId (a GUID), "
            f"not a Graph DriveItem id (the 01… form) — Graph cannot address it, not uploaded"
        )
        return None
    if not _ITEM_ID.match(folder_id):
        print(f"[WARN] annexure | folder id {folder_id!r} is not a DriveItem id — not uploaded")
        return None

    if len(data) > SIMPLE_UPLOAD_LIMIT:
        print(
            f"[WARN] annexure | '{filename}' is {len(data) / 1024 / 1024:.1f} MB, past Graph's "
            f"4 MB simple-upload limit — needs an upload session, not uploaded"
        )
        return None

    try:
        token = _get_app_token(settings)
    except Exception as e:
        print(f"[WARN] annexure | could not get a Graph token: {type(e).__name__}: {e}")
        return None

    url = (
        f"{GRAPH}/drives/{drive_id}/items/{folder_id}:"
        f"/{quote(filename)}:/content"
        f"?@microsoft.graph.conflictBehavior={settings.annexure_conflict_behavior}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    try:
        with httpx.Client(timeout=settings.ms_graph_timeout_sec) as client:
            r = client.put(url, headers=headers, content=data)
            if r.status_code == 403:
                print(
                    "[WARN] annexure | Graph refused the upload (403) — the app registration needs "
                    "Sites.Selected (granted on this library) or Files.ReadWrite.All, as an "
                    "Application permission with admin consent. Not uploaded."
                )
                return None
            if r.status_code == 404:
                print(
                    f"[WARN] annexure | drive or folder not found (404) — check that folder "
                    f"{folder_id!r} lives in drive {drive_id!r}. Not uploaded."
                )
                return None
            r.raise_for_status()
            item = r.json()
    except Exception as e:
        print(f"[WARN] annexure | upload of '{filename}' failed: {type(e).__name__}: {e}")
        return None

    link = item.get("webUrl") or ""
    name = item.get("name") or filename
    if name != filename:
        # conflictBehavior=rename landed it beside an existing file.
        print(f"[INFO] annexure | '{filename}' already existed; saved as '{name}' instead")
    print(f"[INFO] annexure | uploaded '{name}' ({len(data) / 1024:.0f} KB)")
    return link or None
