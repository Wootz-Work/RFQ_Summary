from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def build_drive_service(service_account_json: Path):
    info = json.loads(service_account_json.read_text(encoding="utf-8"))
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False), info


def check_file_access(service, file_id: str, download_sample: bool) -> None:
    metadata = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,size,owners(emailAddress),webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    print("ACCESS_OK=true")
    print(f"file_id={metadata.get('id', '')}")
    print(f"name={metadata.get('name', '')}")
    print(f"mime_type={metadata.get('mimeType', '')}")
    print(f"size={metadata.get('size', '')}")
    print(f"web_view_link={metadata.get('webViewLink', '')}")

    owners = metadata.get("owners") or []
    owner_emails = [o.get("emailAddress", "") for o in owners if o.get("emailAddress")]
    if owner_emails:
        print(f"owners={', '.join(owner_emails)}")

    if not download_sample:
        return

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=256 * 1024)
    _, done = downloader.next_chunk()
    print(f"download_sample_ok=true")
    print(f"downloaded_sample_bytes={len(buffer.getvalue())}")
    print(f"download_done={done}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether a Google service account JSON can read a Google Drive file ID."
    )
    parser.add_argument(
        "--service-account-json",
        default="service_account.json",
        help="Path to the pasted service account JSON file. Default: service_account.json",
    )
    parser.add_argument("--file-id", required=True, help="Google Drive file ID to check.")
    parser.add_argument(
        "--download-sample",
        action="store_true",
        help="Also try downloading the first small chunk into memory. Nothing is written to disk.",
    )
    args = parser.parse_args()

    service_account_json = Path(args.service_account_json)
    if not service_account_json.exists():
        print(f"ACCESS_OK=false")
        print(f"error=service account JSON not found: {service_account_json}")
        return 2

    try:
        service, info = build_drive_service(service_account_json)
        print(f"service_account_email={info.get('client_email', '')}")
        check_file_access(service, args.file_id, args.download_sample)
        return 0
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", "")
        print("ACCESS_OK=false")
        print(f"http_status={status}")
        print(f"error={exc}")
        return 1
    except Exception as exc:
        print("ACCESS_OK=false")
        print(f"error={type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
