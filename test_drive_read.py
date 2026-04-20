"""
test_drive_read.py
──────────────────
Tests one thing: can the service read a file from Google Drive using the service account?

Setup:
    export GOOGLE_SERVICE_ACCOUNT_PATH=./service_account.json
    export TEST_DRIVE_FILE_ID=19GaUlXC8sS8boBzYj-w4aGq99omZlQRJ

Run:
    python test_drive_read.py
"""

import os
import sys
import io


def test_drive_file_is_readable():
    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "service_account.json")
    file_id = os.getenv("TEST_DRIVE_FILE_ID", "")

    # ── Pre-flight checks ────────────────────────────────────────────────────
    if not os.path.exists(sa_path):
        print(f"❌  service_account.json not found at: {sa_path}")
        print(f"    Set GOOGLE_SERVICE_ACCOUNT_PATH env var or place it in the project root.")
        sys.exit(1)

    if not file_id:
        print("❌  TEST_DRIVE_FILE_ID env var is not set.")
        print("    export TEST_DRIVE_FILE_ID=<your Drive file ID>")
        sys.exit(1)

    print(f"  Service account : {sa_path}")
    print(f"  File ID         : {file_id}")
    print()

    # ── Authenticate ─────────────────────────────────────────────────────────
    print("  [1/3] Authenticating with service account...")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        print("        ✅ Authenticated")
    except ImportError:
        print("        ❌ Missing dependencies.")
        print("           Run: pip install google-auth google-auth-httplib2 google-api-python-client")
        sys.exit(1)
    except Exception as e:
        print(f"        ❌ Auth failed: {e}")
        sys.exit(1)

    # ── Fetch metadata ────────────────────────────────────────────────────────
    print("  [2/3] Fetching file metadata...")
    try:
        meta = service.files().get(fileId=file_id, fields="name,mimeType,size").execute()
        print(f"        ✅ Name      : {meta.get('name')}")
        print(f"           MIME type : {meta.get('mimeType')}")
        print(f"           Size      : {meta.get('size', 'unknown')} bytes")
    except Exception as e:
        print(f"        ❌ Metadata fetch failed: {e}")
        print()
        print("  Possible causes:")
        print("    - File ID is wrong")
        print("    - Service account email not added as Viewer on the Drive folder")
        print("    - Google Drive API not enabled in GCP Console")
        sys.exit(1)

    # ── Download bytes ────────────────────────────────────────────────────────
    print("  [3/3] Downloading file bytes...")
    try:
        from googleapiclient.http import MediaIoBaseDownload

        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        total_bytes = len(buffer.getvalue())
        print(f"        ✅ Downloaded : {total_bytes} bytes")
    except Exception as e:
        print(f"        ❌ Download failed: {e}")
        sys.exit(1)

    print()
    print("  ✅  File is readable. Service account has correct access.")


if __name__ == "__main__":
    print("─" * 56)
    print("  Google Drive File Read Test")
    print("─" * 56)
    print()
    test_drive_file_is_readable()
    print("─" * 56)