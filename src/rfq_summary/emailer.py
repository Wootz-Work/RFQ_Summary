"""
Outbound notification mail.

Used by the regenerate flow to tell the people sharing an RFQ that something
material changed. Nothing here may ever fail a run: every path returns a count
and logs, and callers treat a zero as "not sent", never as an error.
"""
from __future__ import annotations

import html
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterable, List

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


def _build_message(
    settings: Settings,
    recipients: List[str],
    rfq_id: str,
    rfq_title: str,
    summary_text: str,
) -> EmailMessage:
    title = (rfq_title or "").strip() or rfq_id or "this RFQ"

    msg = EmailMessage()
    msg["Subject"] = f"Zai updated summary - {title}"[:200]
    msg["From"] = formataddr((settings.email_from_name or "Wootz.Strike", settings.email_from_address))
    msg["To"] = ", ".join(recipients)
    if settings.email_reply_to:
        msg["Reply-To"] = settings.email_reply_to

    greeting = f"Hi folks, Zai summary updated based on the recent changes in the RFQ {title}"
    body = re.sub(r"</?triage>", "", summary_text or "", flags=re.I).strip()

    msg.set_content(f"{greeting}\n\n{body}\n")
    msg.add_alternative(
        f"""<div style="font-family:Arial,Helvetica,sans-serif; background:#f6f7f8; padding:24px;">
  <div style="max-width:640px; margin:0 auto; background:#ffffff; border:1px solid #e0e4e9;
              border-top:3px solid #4a6178; padding:28px 32px;">
    <p style="margin:0 0 20px 0; font-size:15px; line-height:24px; color:#33383f;">
      {html.escape(greeting)}
    </p>
    {_markdown_to_html(body)}
  </div>
</div>""",
        subtype="html",
    )
    return msg


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
    Mail the shared members that something material changed.

    Returns how many addresses it went to; 0 means nothing was sent, which is
    a normal outcome and never an error. Sends only when there is a change to
    report: an unchanged regeneration must not generate mail, or the mail stops
    being read.
    """
    if not settings.enable_regenerate_email:
        return 0

    changed = (changed_text or "").strip()
    if not changed:
        return 0

    recipients = parse_recipients(shared_members)
    if not recipients:
        return 0

    host = (settings.smtp_host or "").strip()
    sender = (settings.email_from_address or "").strip()
    if not host or not sender:
        print(
            "[WARN] email | change to report but SMTP is not configured "
            "(need SMTP_HOST and EMAIL_FROM_ADDRESS) — not sent"
        )
        return 0

    # changed_text decides WHETHER to send; summary_text is WHAT is sent. The
    # summary already carries the change note at its top, so a reader gets the
    # delta first and the full picture underneath.
    msg = _build_message(settings, recipients, rfq_id, rfq_title, (summary_text or "").strip() or changed)

    try:
        if settings.smtp_use_ssl:
            server = smtplib.SMTP_SSL(host, settings.smtp_port, timeout=settings.smtp_timeout_sec,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, settings.smtp_port, timeout=settings.smtp_timeout_sec)
        with server:
            if settings.smtp_use_tls and not settings.smtp_use_ssl:
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except Exception as e:
        # A failed notification is not a failed regeneration.
        print(f"[WARN] email | could not notify {len(recipients)} member(s): {type(e).__name__}: {e}")
        return 0

    print(f"[INFO] email | change notification sent to {len(recipients)} member(s) for rfq={rfq_id}")
    return len(recipients)
