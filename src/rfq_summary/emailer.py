"""
Outbound notification mail — sent through Microsoft Graph, not SMTP.

Used by the regenerate flow to tell the people sharing an RFQ that something
material changed. Nothing here may ever fail a run: every path returns a
count and logs, and callers treat a zero as "not sent", never as an error.

Why Graph and not SMTP: the tenant has SMTP AUTH disabled (the Microsoft
default, and increasingly the only option as basic auth is retired tenant
by tenant), so a password-based send fails with 535 5.7.139 regardless of
how correct the credentials are. Graph's app-only auth sidesteps that
entirely — it authenticates the application itself via a client secret,
never a mailbox password, and does not depend on SMTP being enabled at all.

Setup this needs in Azure AD / Entra ID, once:
  1. App registration (or reuse an existing one).
  2. API permissions -> Microsoft Graph -> *Application* permissions
     (not Delegated — there is no signed-in user here) -> Mail.Send.
  3. Grant admin consent for that permission. Without this step every
     token request succeeds but every send comes back 403 Forbidden.
  4. Certificates & secrets -> new client secret. Put its VALUE (not the
     secret ID) in MS_GRAPH_CLIENT_SECRET.
  5. MS_GRAPH_TENANT_ID and MS_GRAPH_CLIENT_ID come from the app's
     Overview page (Directory/tenant ID, Application/client ID).
  6. EMAIL_FROM_ADDRESS must be a real mailbox in the tenant. An
     unrestricted Mail.Send grant lets the app send as ANY mailbox in the
     tenant — if that is too broad, an admin scopes it down to just this
     one with an application access policy (New-ApplicationAccessPolicy
     in Exchange Online PowerShell); nothing here needs to change either way.
"""
from __future__ import annotations

import html
import re
import time
from typing import Iterable, List

import httpx

from .config import Settings

# Deliberately permissive: the aim is to drop obvious junk ("-", "n/a", a name
# with no @) before handing a list to the SMTP server, not to police addresses.
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def parse_recipients(raw: object) -> List[str]:
    """
    Split a shared-members value into addresses.

    Accepts a comma-separated string, a semicolon-separated one, a newline
    separated one, or a list. Trims, drops anything that is not shaped like an
    address, and de-duplicates case-insensitively while keeping the first
    spelling seen.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: Iterable[str] = re.split(r"[,;\n]", raw)
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(x) for x in raw]
    else:
        candidates = [str(raw)]

    out: List[str] = []
    seen = set()
    for item in candidates:
        addr = (item or "").strip().strip("<>").strip()
        if not addr or not _EMAIL_RE.match(addr):
            continue
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            out.append(addr)
    return out


def _markdown_to_html(text: str) -> str:
    """
    Render the markdown the triage and diff prompts emit: `####` headings,
    `-` bullets, `**bold**`, `*italic*`, `` `code` ``, `---` rules and pipe
    tables.

    Everything is escaped before any markup is added, so neither the model's
    text nor a customer-supplied RFQ title can inject HTML into the mail.
    """
    def inline(raw: str) -> str:
        safe = html.escape(raw)
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        safe = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", safe)
        safe = re.sub(r"`(.+?)`", r"<code>\1</code>", safe)
        return safe

    # The triage body arrives inside its tag; the tag is plumbing, not content.
    text = re.sub(r"</?triage>", "", text or "", flags=re.I)

    P = 'margin:0 0 12px 0; font-size:15px; line-height:24px; color:#33383f;'
    parts: List[str] = []
    lines = [l.rstrip() for l in text.splitlines()]
    i, in_list = 0, False

    def close_list():
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Pipe table: a header row, a separator row, then body rows.
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            close_list()
            def cells(row):
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(line)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(cells(lines[i].strip()))
                i += 1
            th = "".join(
                f'<th style="text-align:left; padding:8px 10px; border-bottom:1px solid #d5dae1; '
                f'font-size:13px; color:#4a6178;">{inline(c)}</th>' for c in head
            )
            rows = "".join(
                "<tr>" + "".join(
                    f'<td style="padding:8px 10px; border-bottom:1px solid #eceff3; '
                    f'font-size:14px; line-height:21px; color:#33383f; vertical-align:top;">{inline(c)}</td>'
                    for c in r
                ) + "</tr>"
                for r in body
            )
            parts.append(
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                'style="width:100%; border-collapse:collapse; margin:0 0 14px 0;">'
                f"<tr>{th}</tr>{rows}</table>"
            )
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line):
            close_list()
            parts.append('<div style="height:1px; background:#e0e4e9; margin:16px 0;"></div>')
        elif line.startswith("#"):
            close_list()
            level = len(line) - len(line.lstrip("#"))
            size = 18 if level <= 3 else 16
            parts.append(
                f'<p style="margin:16px 0 10px 0; font-size:{size}px; font-weight:bold; '
                f'color:#1f2328;">{inline(line.lstrip("# ").strip())}</p>'
            )
        elif line.startswith(("- ", "* ")):
            if not in_list:
                parts.append('<ul style="margin:0 0 12px 0; padding-left:20px;">')
                in_list = True
            parts.append(
                f'<li style="margin:0 0 8px 0; font-size:15px; line-height:24px; '
                f'color:#33383f;">{inline(line[2:])}</li>'
            )
        else:
            close_list()
            parts.append(f'<p style="{P}">{inline(line)}</p>')
        i += 1

    close_list()
    return "\n".join(parts)


def _build_graph_message(
    settings: Settings,
    recipients: List[str],
    rfq_id: str,
    rfq_title: str,
    summary_text: str,
) -> dict:
    """
    The Graph sendMail request body. Graph carries one body per message —
    unlike SMTP's multipart/alternative there is no separate plain-text part,
    so the HTML rendering is what every recipient sees, in every client.
    """
    title = (rfq_title or "").strip() or rfq_id or "this RFQ"
    subject = f"Zai updated summary - {title}"[:255]

    greeting = f"Hi folks, Zai summary updated based on the recent changes in the RFQ {title}"
    body = re.sub(r"</?triage>", "", summary_text or "", flags=re.I).strip()

    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif; background:#f6f7f8; padding:24px;">
  <div style="max-width:640px; margin:0 auto; background:#ffffff; border:1px solid #e0e4e9;
              border-top:3px solid #4a6178; padding:28px 32px;">
    <p style="margin:0 0 20px 0; font-size:15px; line-height:24px; color:#33383f;">
      {html.escape(greeting)}
    </p>
    {_markdown_to_html(body)}
  </div>
</div>"""

    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
    }
    if settings.email_reply_to:
        message["replyTo"] = [{"emailAddress": {"address": settings.email_reply_to}}]

    return {"message": message, "saveToSentItems": settings.ms_graph_save_to_sent_items}


# (tenant_id, client_id) -> {"token": str, "expires_at": float (time.monotonic seconds)}
# Module-level and process-lifetime: the server runs as one long-lived
# process, so refetching a token on every send would be one extra round
# trip per mail for no reason. Refreshed automatically once it is close to
# expiry (Graph app tokens are typically valid ~60-90 minutes).
_token_cache: dict = {}


def _graph_configured(settings: Settings) -> bool:
    return bool(
        (settings.ms_graph_tenant_id or "").strip()
        and (settings.ms_graph_client_id or "").strip()
        and (settings.ms_graph_client_secret or "").strip()
        and (settings.email_from_address or "").strip()
    )


def _get_app_token(settings: Settings) -> str:
    """
    Client-credentials (app-only) token for Graph. No user ever signs in —
    the application authenticates as itself via its client secret, which is
    exactly what makes this immune to the tenant's SMTP-AUTH block.

    Cached until ~60s before expiry; raises on failure so the caller can
    distinguish "could not get a token" from "got a token, send failed".
    """
    key = (settings.ms_graph_tenant_id, settings.ms_graph_client_id)
    cached = _token_cache.get(key)
    now = time.monotonic()
    if cached and cached["expires_at"] - now > 60:
        return cached["token"]

    url = f"https://login.microsoftonline.com/{settings.ms_graph_tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": settings.ms_graph_client_id,
        "client_secret": settings.ms_graph_client_secret,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    with httpx.Client(timeout=settings.ms_graph_timeout_sec) as client:
        r = client.post(url, data=data)
        r.raise_for_status()
        payload = r.json()

    token = payload["access_token"]
    expires_in = int(payload.get("expires_in") or 3599)
    _token_cache[key] = {"token": token, "expires_at": now + expires_in}
    return token


def send_change_notification(
    settings: Settings,
    rfq_id: str,
    rfq_title: str,
    changed_text: str,
    shared_members: object,
    requested_by: str = "",
    summary_text: str = "",
) -> int:
    """
    Mail the shared members that something material changed, via Microsoft
    Graph's application-permission sendMail — never SMTP.

    Returns how many addresses it went to; 0 means nothing was sent, which is
    a normal outcome and never an error. Sends only when there is a change to
    report: an unchanged regeneration must not generate mail, or the mail
    stops being read.
    """
    if not settings.enable_regenerate_email:
        return 0

    changed = (changed_text or "").strip()
    if not changed:
        return 0

    recipients = parse_recipients(shared_members)
    if not recipients:
        return 0

    if not _graph_configured(settings):
        print(
            "[WARN] email | change to report but Microsoft Graph is not configured "
            "(need MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET, "
            "EMAIL_FROM_ADDRESS) — not sent"
        )
        return 0

    # changed_text decides WHETHER to send; summary_text is WHAT is sent. The
    # summary already carries the change note at its top, so a reader gets the
    # delta first and the full picture underneath.
    payload = _build_graph_message(settings, recipients, rfq_id, rfq_title,
                                    (summary_text or "").strip() or changed)

    sender = settings.email_from_address.strip()
    try:
        token = _get_app_token(settings)
    except Exception as e:
        # A failed notification is not a failed regeneration.
        print(f"[WARN] email | could not get a Graph token: {type(e).__name__}: {e}")
        return 0

    url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=settings.ms_graph_timeout_sec) as client:
            r = client.post(url, headers=headers, json=payload)
            # 403 here almost always means admin consent was never granted on
            # the Mail.Send *application* permission — the token is valid,
            # the app just is not allowed to send. Worth naming, since the
            # generic exception text alone sends people down the wrong path.
            if r.status_code == 403:
                print(
                    f"[WARN] email | Graph refused to send (403) — check that Mail.Send is an "
                    f"Application permission with admin consent granted, and that {sender!r} is "
                    f"allowed by any application access policy. Body: {r.text[:300]}"
                )
                return 0
            r.raise_for_status()
    except Exception as e:
        print(f"[WARN] email | could not notify {len(recipients)} member(s) via Graph: {type(e).__name__}: {e}")
        return 0

    print(f"[INFO] email | change notification sent to {len(recipients)} member(s) for rfq={rfq_id} via Graph")
    return len(recipients)
