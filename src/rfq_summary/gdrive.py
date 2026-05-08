from __future__ import annotations

import base64
import io
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Tuple, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


@lru_cache(maxsize=1)
def _get_drive_service(service_account_path: str, service_account_b64: str):
    if service_account_path and Path(service_account_path).exists():
        creds = service_account.Credentials.from_service_account_file(
            service_account_path, scopes=SCOPES
        )
    else:
        encoded = (
            service_account_b64
            or os.getenv("GOOGLE_DRIVE_SA_JSON_B64")
            or ""
        ).strip()
        if not encoded:
            raise RuntimeError(
                "Missing Google Drive service account credentials: set "
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_PATH or GOOGLE_DRIVE_SA_JSON_B64."
            )
        info = json.loads(base64.b64decode(encoded).decode("utf-8"))
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def fetch_drive_file(
    file_id: str,
    service_account_path: str,
    service_account_b64: str = "",
) -> Tuple[bytes, Optional[str]]:
    service = _get_drive_service(service_account_path, service_account_b64)

    # Get metadata first (for mime type)
    meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
    mime_type = meta.get("mimeType", "")

    # Download file bytes
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    return buffer.getvalue(), mime_type
