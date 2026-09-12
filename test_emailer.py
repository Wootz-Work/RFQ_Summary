"""
test_emailer.py — the change-notification mail, without an SMTP server.

The rules that matter: it fires only on a real change, only to valid
addresses, and it can never take a regeneration down.

Run:
    python test_emailer.py
"""
import sys
sys.path.insert(0, "src")

import smtplib

from rfq_summary.config import Settings
from rfq_summary.emailer import _build_message, parse_recipients, send_change_notification

CHANGED = "#### What changed since the last version\n- **Coating** — was zinc, now zinc flake. Reprice."


def _check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    return bool(cond)


class _Server:
    """Stands in for smtplib.SMTP, recording what would have gone out."""
    sent = []
    logins = []
    tls = []

    def __init__(self, host, port, timeout=None): self.host, self.port = host, port
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, context=None): _Server.tls.append(True)
    def login(self, u, p): _Server.logins.append(u)
    def send_message(self, msg): _Server.sent.append(msg)


def _settings(**kw):
    base = dict(GLIDE_API_KEY="k", GLIDE_APP_ID="a", SMTP_HOST="smtp.office365.com",
                EMAIL_FROM_ADDRESS="technology@wootz.work")
    base.update(kw)
    return Settings(**base)


def _with_stub(fn):
    real = smtplib.SMTP
    smtplib.SMTP = _Server
    _Server.sent, _Server.logins, _Server.tls = [], [], []
    try:
        return fn()
    finally:
        smtplib.SMTP = real


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
    ok &= _check("cleared SMTP host sends nothing",
                 go(CHANGED, "a@b.com", _settings(SMTP_HOST="")) == 0)
    ok &= _check("cleared from address sends nothing",
                 go(CHANGED, "a@b.com", _settings(EMAIL_FROM_ADDRESS="")) == 0)
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
    msg = _build_message(s, ["a@wootz.work", "b@wootz.work"], "R1", "FRUITLAND 46", summary)

    ok = _check("subject is the agreed line", msg["Subject"] == "Zai updated summary - FRUITLAND 46",
                msg["Subject"])
    ok &= _check("from address is technology@", "technology@wootz.work" in msg["From"], msg["From"])
    ok &= _check("from name is Wootz.Strike", "Wootz.Strike" in msg["From"], msg["From"])
    ok &= _check("recipients are in To", msg["To"] == "a@wootz.work, b@wootz.work", str(msg["To"]))
    ok &= _check("nothing is Bcc'd", msg["Bcc"] is None, str(msg["Bcc"]))
    ok &= _check("reply-to honoured", msg["Reply-To"] == "rfq@wootz.work")

    plain = msg.get_body("plain").get_content()
    ok &= _check("greeting is the agreed line",
                 plain.startswith("Hi folks, Zai summary updated based on the recent changes in the RFQ FRUITLAND 46"),
                 plain[:120])
    ok &= _check("the triage tag never reaches the reader", "<triage>" not in plain)

    html_body = msg.get_body("html").get_content()
    ok &= _check("the change note is carried", "Coating" in html_body)
    ok &= _check("the full summary is carried", "Drilling hours dominate" in html_body)
    ok &= _check("the triage table renders as a table", "<table" in html_body and "<th" in html_body)
    ok &= _check("bold renders", "<strong>Cost</strong>" in html_body)
    ok &= _check("bullets render", "<li" in html_body)
    ok &= _check("no stray triage tag in html", "&lt;triage&gt;" not in html_body and "<triage>" not in html_body)

    # Model output and customer titles must not be able to inject markup.
    evil = _build_message(s, ["a@b.com"], "R1", "<script>alert(1)</script>",
                          "- **x** <img src=x onerror=alert(1)>")
    ehtml = evil.get_body("html").get_content()
    ok &= _check("title is escaped", "<script>" not in ehtml)
    ok &= _check("summary is escaped", "<img src=x" not in ehtml)

    # Falling back to the id when no title came through.
    bare = _build_message(s, ["a@b.com"], "RFQ-77", "", "- something")
    ok &= _check("id stands in for a missing title", "RFQ-77" in bare["Subject"], bare["Subject"])
    return ok


def test_never_breaks_the_run():
    def boom(*a, **k):
        raise smtplib.SMTPException("relay refused")

    real = smtplib.SMTP
    smtplib.SMTP = boom
    try:
        n = send_change_notification(_settings(), "R1", "T", CHANGED, "a@b.com", "Ayush", "summary")
    finally:
        smtplib.SMTP = real
    ok = _check("SMTP failure returns 0 rather than raising", n == 0)

    n = send_change_notification(_settings(), "R1", "T", CHANGED, object(), "Ayush", "summary")
    ok &= _check("a nonsense members value is survivable", n == 0)
    return ok


def test_auth_and_tls():
    _with_stub(lambda: send_change_notification(
        _settings(SMTP_USER="u", SMTP_PASSWORD="p"), "R1", "T", CHANGED, "a@b.com"))
    ok = _check("logs in when a user is set", _Server.logins == ["u"], str(_Server.logins))
    ok &= _check("starts TLS by default", _Server.tls == [True])

    _with_stub(lambda: send_change_notification(
        _settings(SMTP_USE_TLS="false"), "R1", "T", CHANGED, "a@b.com"))
    ok &= _check("TLS can be turned off", _Server.tls == [])
    ok &= _check("no login without a user", _Server.logins == [])
    return ok


if __name__ == "__main__":
    passed = all([test_recipients(), test_sends_only_on_change(), test_message_shape(),
                  test_never_breaks_the_run(), test_auth_and_tls()])
    print("\nALL PASSED" if passed else "\nFAILURES ABOVE")
    raise SystemExit(0 if passed else 1)
