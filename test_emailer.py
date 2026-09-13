"""
test_emailer.py — the change-notification mail via Microsoft Graph, without
ever making a real HTTP call.

The rules that matter: it fires only on a real change, only to valid
addresses, it authenticates as the application (never a mailbox password),
and no failure mode — token, send, or config — can take a regeneration down.

Run:
    python test_emailer.py
"""
import sys
sys.path.insert(0, "src")

import httpx

from rfq_summary import emailer
from rfq_summary.config import Settings
from rfq_summary.emailer import _build_graph_message, parse_recipients, send_change_notification

CHANGED = "#### What changed since the last version\n- **Coating** — was zinc, now zinc flake. Reprice."


def _check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    return bool(cond)


def _settings(**kw):
    base = dict(
        GLIDE_API_KEY="k", GLIDE_APP_ID="a",
        MS_GRAPH_TENANT_ID="tenant-1", MS_GRAPH_CLIENT_ID="client-1",
        MS_GRAPH_CLIENT_SECRET="secret-1", EMAIL_FROM_ADDRESS="technology@wootz.work",
    )
    base.update(kw)
    return Settings(**base)


class _Resp:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _Client:
    """Stands in for httpx.Client, recording every request made through it."""
    calls = []
    # Queue of responses to hand out, in order; a token call and a send call
    # each pop one. Reset per test via _reset().
    queue = []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post(self, url, headers=None, json=None, data=None):
        _Client.calls.append({"url": url, "headers": headers, "json": json, "data": data})
        if _Client.queue:
            return _Client.queue.pop(0)
        return _Resp(200, {"access_token": "tok-1", "expires_in": 3600})


def _reset(queue=None):
    _Client.calls = []
    _Client.queue = list(queue or [])
    emailer._token_cache.clear()


def _with_stub(fn, queue=None):
    _reset(queue)
    real = httpx.Client
    httpx.Client = _Client
    try:
        return fn()
    finally:
        httpx.Client = real


def test_recipients():
    ok = _check("comma separated", parse_recipients("a@b.com, c@d.com") == ["a@b.com", "c@d.com"])
    ok &= _check("semicolons and newlines", parse_recipients("a@b.com;c@d.com\ne@f.com") ==
                 ["a@b.com", "c@d.com", "e@f.com"])
    ok &= _check("junk dropped", parse_recipients("a@b.com, n/a, -, notanemail") == ["a@b.com"])
    ok &= _check("case-insensitive dedupe", parse_recipients("a@b.com, A@B.com") == ["a@b.com"])
    ok &= _check("angle brackets trimmed", parse_recipients("<a@b.com>") == ["a@b.com"])
    ok &= _check("a list works too", parse_recipients(["a@b.com", "bad"]) == ["a@b.com"])
    ok &= _check("empty is empty", parse_recipients("") == [] and parse_recipients(None) == [])
    return ok


def test_sends_only_on_change():
    def go(changed, members, settings=None):
        return _with_stub(lambda: send_change_notification(
            settings or _settings(), "R1", "Duplex tubesheet", changed, members, "Ayush",
            summary_text="<triage>full updated summary</triage>"))

    ok = _check("a real change sends", go(CHANGED, "a@b.com, c@d.com") == 2)
    ok &= _check("no change sends nothing", go("", "a@b.com") == 0)
    ok &= _check("whitespace-only change sends nothing", go("   \n ", "a@b.com") == 0)
    ok &= _check("no members sends nothing", go(CHANGED, "") == 0)
    ok &= _check("only junk members sends nothing", go(CHANGED, "n/a, -") == 0)
    ok &= _check("kill switch", go(CHANGED, "a@b.com", _settings(ENABLE_REGENERATE_EMAIL="false")) == 0)
    ok &= _check("missing tenant id sends nothing",
                 go(CHANGED, "a@b.com", _settings(MS_GRAPH_TENANT_ID="")) == 0)
    ok &= _check("missing client secret sends nothing",
                 go(CHANGED, "a@b.com", _settings(MS_GRAPH_CLIENT_SECRET="")) == 0)
    ok &= _check("missing sender sends nothing",
                 go(CHANGED, "a@b.com", _settings(EMAIL_FROM_ADDRESS="")) == 0)
    return ok


def test_uses_app_only_auth_never_a_password():
    """The whole point of switching to Graph: no mailbox password anywhere."""
    s = _settings()
    _with_stub(lambda: send_change_notification(s, "R1", "T", CHANGED, "a@b.com",
                                                 summary_text="s"))
    token_call = _Client.calls[0]
    ok = _check("token request hits the tenant's v2 endpoint",
                token_call["url"] == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token",
                token_call["url"])
    ok &= _check("grant type is client_credentials",
                 token_call["data"]["grant_type"] == "client_credentials", str(token_call["data"]))
    ok &= _check("scope is the Graph default scope",
                 token_call["data"]["scope"] == "https://graph.microsoft.com/.default")
    ok &= _check("client secret travels in the token request, not a mailbox password",
                 token_call["data"]["client_secret"] == "secret-1")
    ok &= _check("no field named password anywhere in the request", "password" not in str(token_call))

    send_call = _Client.calls[1]
    ok &= _check("send goes to the sender mailbox's sendMail endpoint",
                 send_call["url"] == "https://graph.microsoft.com/v1.0/users/technology@wootz.work/sendMail",
                 send_call["url"])
    ok &= _check("bearer token is attached", send_call["headers"]["Authorization"] == "Bearer tok-1")
    return ok


def test_token_is_cached_and_refreshed():
    s = _settings()

    def two_sends():
        send_change_notification(s, "R1", "T", CHANGED, "a@b.com", summary_text="s")
        send_change_notification(s, "R2", "T", CHANGED, "a@b.com", summary_text="s")

    _with_stub(two_sends)
    token_calls = [c for c in _Client.calls if "oauth2" in c["url"]]
    ok = _check("one token request serves two sends", len(token_calls) == 1, str(len(token_calls)))

    # A near-expired cached token triggers a real refresh.
    emailer._token_cache[("tenant-1", "client-1")] = {"token": "stale", "expires_at": 0.0}
    _with_stub(lambda: send_change_notification(s, "R3", "T", CHANGED, "a@b.com", summary_text="s"))
    ok &= _check("an expired token is refreshed, not reused",
                 any("oauth2" in c["url"] for c in _Client.calls))
    return ok


def test_message_shape():
    s = _settings(EMAIL_REPLY_TO="rfq@wootz.work")
    summary = (
        "#### What changed since the last version\n"
        "- **Coating** — was zinc, now zinc flake. Reprice the finish.\n\n"
        "<triage>\n"
        "**Duplex tubesheet package. Drilling hours dominate.**\n\n"
        "| Description | Value | Sensitivity |\n"
        "|---|---|---|\n"
        "| **Cost** | $xx,xxx | 371 holes assumed |\n"
        "</triage>"
    )
    payload = _build_graph_message(s, ["a@wootz.work", "b@wootz.work"], "R1", "FRUITLAND 46", summary)
    msg = payload["message"]

    ok = _check("subject is the agreed line", msg["subject"] == "Zai updated summary - FRUITLAND 46",
                msg["subject"])
    ok &= _check("body content type is HTML", msg["body"]["contentType"] == "HTML")
    ok &= _check("recipients are addressed correctly",
                 [r["emailAddress"]["address"] for r in msg["toRecipients"]] ==
                 ["a@wootz.work", "b@wootz.work"])
    ok &= _check("reply-to honoured",
                 msg["replyTo"][0]["emailAddress"]["address"] == "rfq@wootz.work")
    ok &= _check("saveToSentItems defaults true", payload["saveToSentItems"] is True)

    html_body = msg["body"]["content"]
    ok &= _check("greeting is the agreed line",
                 "Hi folks, Zai summary updated based on the recent changes in the RFQ FRUITLAND 46"
                 in html_body, html_body[:160])
    ok &= _check("the change note is carried", "Coating" in html_body)
    ok &= _check("the full summary is carried", "Drilling hours dominate" in html_body)
    ok &= _check("the triage tag never reaches the reader", "<triage>" not in html_body)
    ok &= _check("the triage table renders as a table", "<table" in html_body and "<th" in html_body)
    ok &= _check("bold renders", "<strong>Cost</strong>" in html_body)
    ok &= _check("bullets render", "<li" in html_body)

    # Model output and customer titles must not be able to inject markup.
    evil = _build_graph_message(s, ["a@b.com"], "R1", "<script>alert(1)</script>",
                                 "- **x** <img src=x onerror=alert(1)>")
    ehtml = evil["message"]["body"]["content"]
    ok &= _check("title is escaped", "<script>" not in ehtml)
    ok &= _check("summary is escaped", "<img src=x" not in ehtml)

    bare = _build_graph_message(s, ["a@b.com"], "RFQ-77", "", "- something")
    ok &= _check("id stands in for a missing title", "RFQ-77" in bare["message"]["subject"],
                 bare["message"]["subject"])
    return ok


def test_never_breaks_the_run():
    # Token endpoint itself fails.
    n = _with_stub(
        lambda: send_change_notification(_settings(), "R1", "T", CHANGED, "a@b.com", summary_text="s"),
        queue=[_Resp(500, text="tenant unreachable")],
    )
    ok = _check("a failed token request returns 0 rather than raising", n == 0)

    # Token succeeds, the send itself is refused with a plain 5xx/4xx.
    n = _with_stub(
        lambda: send_change_notification(_settings(), "R1", "T", CHANGED, "a@b.com", summary_text="s"),
        queue=[_Resp(200, {"access_token": "tok", "expires_in": 3600}), _Resp(500, text="send failed")],
    )
    ok &= _check("a failed send returns 0 rather than raising", n == 0)

    # The 403-that-means-no-admin-consent path, specifically.
    n = _with_stub(
        lambda: send_change_notification(_settings(), "R1", "T", CHANGED, "a@b.com", summary_text="s"),
        queue=[_Resp(200, {"access_token": "tok", "expires_in": 3600}),
               _Resp(403, text="Forbidden: insufficient privileges")],
    )
    ok &= _check("a 403 (missing admin consent) returns 0 rather than raising", n == 0)

    # A nonsense members value.
    n = _with_stub(lambda: send_change_notification(_settings(), "R1", "T", CHANGED, object(),
                                                     summary_text="s"))
    ok &= _check("a nonsense members value is survivable", n == 0)
    return ok


if __name__ == "__main__":
    passed = all([
        test_recipients(),
        test_sends_only_on_change(),
        test_uses_app_only_auth_never_a_password(),
        test_token_is_cached_and_refreshed(),
        test_message_shape(),
        test_never_breaks_the_run(),
    ])
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    raise SystemExit(0 if passed else 1)
