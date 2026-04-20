from __future__ import annotations

import io
from functools import lru_cache
from typing import Tuple, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


@lru_cache(maxsize=1)
def _get_drive_service(service_account_path: str):
    creds = service_account.Credentials.from_service_account_file(
        service_account_path, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def fetch_drive_file(file_id: str, service_account_path: str) -> Tuple[bytes, Optional[str]]:
    service = _get_drive_service(service_account_path)

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