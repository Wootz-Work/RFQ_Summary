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
    base = dict(GLIDE_API_KEY="k", GLIDE_APP_ID="a", SMTP_HOST="smtp.example.com",
                EMAIL_FROM_ADDRESS="strike@wootz.work")
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
            settings or _settings(), "R1", "Duplex tubesheet", changed, members, "Ayush"))

    ok = _check("a real change sends", go(CHANGED, "a@b.com, c@d.com") == 2)
    ok &= _check("no change sends nothing", go("", "a@b.com") == 0)
    ok &= _check("whitespace-only change sends nothing", go("   \n ", "a@b.com") == 0)
    ok &= _check("no members sends nothing", go(CHANGED, "") == 0)
    ok &= _check("only junk members sends nothing", go(CHANGED, "n/a, -") == 0)
    ok &= _check("kill switch", go(CHANGED, "a@b.com", _settings(ENABLE_REGENERATE_EMAIL="false")) == 0)
    ok &= _check("unconfigured SMTP sends nothing",
                 go(CHANGED, "a@b.com", Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a")) == 0)
    return ok


def test_message_shape():
    s = _settings(EMAIL_REPLY_TO="rfq@wootz.work")
    msg = _build_message(s, ["a@b.com", "c@d.com"], "R1", "Duplex tubesheet", CHANGED, "Ayush")

    ok = _check("from name is Wootz.Strike", "Wootz.Strike" in msg["From"], msg["From"])
    ok &= _check("from address used", "strike@wootz.work" in msg["From"])
    ok &= _check("recipients are Bcc, not To", "a@b.com" in msg["Bcc"] and "a@b.com" not in msg["To"],
                 f"To={msg['To']} Bcc={msg['Bcc']}")
    ok &= _check("reply-to honoured", msg["Reply-To"] == "rfq@wootz.work")
    ok &= _check("subject names the RFQ", "Duplex tubesheet" in msg["Subject"], msg["Subject"])
    ok &= _check("requester attributed", "Ayush" in msg.get_body("plain").get_content())

    html = msg.get_body("html").get_content()
    ok &= _check("change text is in the html", "Coating" in html)
    ok &= _check("markdown bold rendered", "<strong>Coating</strong>" in html, html[:200])
    ok &= _check("bullets rendered", "<li" in html)

    # Model output and customer titles must not be able to inject markup.
    evil = _build_message(s, ["a@b.com"], "R1", "<script>alert(1)</script>",
                          "- **x** <img src=x onerror=alert(1)>", "")
    ehtml = evil.get_body("html").get_content()
    ok &= _check("title is escaped", "<script>" not in ehtml)
    ok &= _check("change text is escaped", "<img src=x" not in ehtml)
    ok &= _check("missing requester degrades", "regenerated" in evil.get_body("plain").get_content().lower())
    return ok


def test_never_breaks_the_run():
    def boom(*a, **k):
        raise smtplib.SMTPException("relay refused")

    real = smtplib.SMTP
    smtplib.SMTP = boom
    try:
        n = send_change_notification(_settings(), "R1", "T", CHANGED, "a@b.com", "Ayush")
    finally:
        smtplib.SMTP = real
    ok = _check("SMTP failure returns 0 rather than raising", n == 0)

    n = send_change_notification(_settings(), "R1", "T", CHANGED, object(), "Ayush")
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
