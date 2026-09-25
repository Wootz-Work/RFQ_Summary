"""
test_onedrive.py — uploading a generated annexure through Microsoft Graph,
without making a real HTTP call.

The rules that matter: a folder id that Graph cannot address is refused with
a message that names why, a regeneration never overwrites someone's edits,
and no failure mode here can take an extraction down.

Run:
    python test_onedrive.py
"""
import sys

sys.path.insert(0, "src")

import httpx

from rfq_summary import emailer, onedrive
from rfq_summary.config import Settings
from rfq_summary.onedrive import upload_annexure, upload_configured

ITEM = "01DDVW3I6ABM4C6R67VZDLDASFKPWZK3PY"      # a real DriveItem id shape
DRIVE = "b!TESTDRIVE"                             # comes off the RFQ row now
GUID = "f2819368-d725-44fe-962f-07a97b0922d3"    # a SharePoint UniqueId
XLSX = b"PK\x03\x04 pretend workbook"

ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + label + (f"  — {detail}" if detail and not cond else ""))


def _settings(**kw):
    base = dict(
        GLIDE_API_KEY="k", GLIDE_APP_ID="a",
        MS_GRAPH_TENANT_ID="t", MS_GRAPH_CLIENT_ID="c", MS_GRAPH_CLIENT_SECRET="s",
        EMAIL_FROM_ADDRESS="technology@wootz.work",
        MS_GRAPH_DRIVE_ID="b!TESTDRIVE",
    )
    base.update(kw)
    return Settings(**base)


class _Resp:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code, self._json, self.text = status_code, json_body or {}, text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


class _Client:
    calls, queue = [], []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post(self, url, headers=None, json=None, data=None):
        _Client.calls.append({"verb": "POST", "url": url, "data": data})
        return _Resp(200, {"access_token": "tok", "expires_in": 3600})

    def put(self, url, headers=None, content=None):
        _Client.calls.append({"verb": "PUT", "url": url, "headers": headers, "content": content})
        if _Client.queue:
            return _Client.queue.pop(0)
        return _Resp(201, {"name": "Annexure 1 - Hex Bolts.xlsx",
                           "webUrl": "https://wootz-my.sharepoint.com/x/Annexure%201.xlsx"})


def run(fn, queue=None):
    _Client.calls, _Client.queue = [], list(queue or [])
    emailer._token_cache.clear()
    real = httpx.Client
    httpx.Client = _Client
    onedrive.httpx.Client = _Client
    try:
        return fn()
    finally:
        httpx.Client = real
        onedrive.httpx.Client = real


# ---- configuration gate ----------------------------------------------------
check("configured on graph creds alone — drive arrives per RFQ", upload_configured(_settings()))
check("no drive setting is fine; the RFQ row supplies it",
      upload_configured(_settings(MS_GRAPH_DRIVE_ID="")))
check("kill switch respected", not upload_configured(_settings(ENABLE_ANNEXURE_UPLOAD="false")))
check("missing client secret means not configured",
      not upload_configured(_settings(MS_GRAPH_CLIENT_SECRET="")))
check("no drive anywhere returns None rather than raising",
      run(lambda: upload_annexure(_settings(MS_GRAPH_DRIVE_ID=""), "", ITEM, "a.xlsx", XLSX)) is None)
check("the configured drive is used as a fallback when the row has none",
      run(lambda: upload_annexure(_settings(MS_GRAPH_DRIVE_ID="b!FALLBACK"), "", ITEM, "a.xlsx", XLSX))
      is not None)
check("and the fallback drive is the one addressed",
      "/drives/b!FALLBACK/" in [c for c in _Client.calls if c["verb"] == "PUT"][0]["url"])
check("a drive on the row overrides the configured fallback",
      "/drives/b!TESTDRIVE/" in run(lambda: (
          upload_annexure(_settings(MS_GRAPH_DRIVE_ID="b!FALLBACK"), DRIVE, ITEM, "a.xlsx", XLSX),
          [c for c in _Client.calls if c["verb"] == "PUT"][0]["url"])[1]))


# ---- the happy path --------------------------------------------------------
link = run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "Annexure 1 - Hex Bolts.xlsx", XLSX))
check("returns the link to the uploaded file", link and link.startswith("https://"), str(link))
put = [c for c in _Client.calls if c["verb"] == "PUT"][0]
check("uploads into the drive from the RFQ row", "/drives/b!TESTDRIVE/" in put["url"], put["url"])
check("into the folder from the RFQ row", f"/items/{ITEM}:" in put["url"], put["url"])
check("the filename is url-encoded in the path",
      "Annexure%201%20-%20Hex%20Bolts.xlsx" in put["url"], put["url"])
check("the workbook bytes are the body", put["content"] == XLSX)
check("sent as an xlsx content type",
      "spreadsheetml" in put["headers"]["Content-Type"], put["headers"]["Content-Type"])
check("bearer token attached", put["headers"]["Authorization"] == "Bearer tok")


# ---- never overwrite someone's edits ---------------------------------------
check("conflicts rename by default",
      "conflictBehavior=rename" in put["url"], put["url"])
link = run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "a.xlsx", XLSX),
           queue=[_Resp(201, {"name": "a 1.xlsx", "webUrl": "https://x/a%201.xlsx"})])
check("a renamed upload still returns its own link", link == "https://x/a%201.xlsx", str(link))
check("replace is available when explicitly chosen",
      "conflictBehavior=replace" in run(
          lambda: (upload_annexure(_settings(ANNEXURE_CONFLICT_BEHAVIOR="replace"), DRIVE, ITEM, "a.xlsx", XLSX),
                   [c for c in _Client.calls if c["verb"] == "PUT"][0]["url"])[1]))


# ---- folder ids Graph cannot address ---------------------------------------
called = []


def _spy():
    _Client.calls = []
    return upload_annexure(_settings(), DRIVE, GUID, "a.xlsx", XLSX)


check("a SharePoint UniqueId (GUID) is refused, not sent", run(_spy) is None)
check("and no request was attempted", not [c for c in _Client.calls if c["verb"] == "PUT"])
check("an empty folder id is refused",
      run(lambda: upload_annexure(_settings(), DRIVE, "", "a.xlsx", XLSX)) is None)
check("junk is refused",
      run(lambda: upload_annexure(_settings(), DRIVE, "not an id", "a.xlsx", XLSX)) is None)


# ---- size -------------------------------------------------------------------
big = b"x" * (5 * 1024 * 1024)
check("past Graph's 4 MB simple-upload limit is refused rather than truncated",
      run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "big.xlsx", big)) is None)


# ---- nothing here can fail the run -----------------------------------------
check("a 403 (no Files/Sites permission) returns None",
      run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "a.xlsx", XLSX),
          queue=[_Resp(403, text="forbidden")]) is None)
check("a 404 (wrong drive or folder) returns None",
      run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "a.xlsx", XLSX),
          queue=[_Resp(404, text="not found")]) is None)
check("a 500 returns None",
      run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "a.xlsx", XLSX),
          queue=[_Resp(500, text="boom")]) is None)


def _token_fails(*a, **k):
    raise RuntimeError("tenant unreachable")


real_token = onedrive._get_app_token
onedrive._get_app_token = _token_fails
try:
    check("a failed token returns None rather than raising",
          run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "a.xlsx", XLSX)) is None)
finally:
    onedrive._get_app_token = real_token

check("a response with no webUrl returns None",
      run(lambda: upload_annexure(_settings(), DRIVE, ITEM, "a.xlsx", XLSX),
          queue=[_Resp(201, {"name": "a.xlsx"})]) is None)

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
raise SystemExit(0 if ok else 1)
