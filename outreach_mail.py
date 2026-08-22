"""Composing and sending outreach mail.

One module owns the envelope, the signature and the legal footer, because the
alternative is three modules each getting some of it right. Nothing here sends
on a schedule; callers decide when.

Sending is off unless SMTP_HOST, SMTP_USER and SMTP_PASS are all set. That is
deliberate: an unconfigured deployment must no-op rather than raise, so the
review queue can be exercised long before mail is live.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Dict


STORE_BASE_URL = "https://stellasageco.com"

# CAN-SPAM requires a physical postal address in commercial email, and its
# absence is one of the things that makes a real offer read as a scam.
MAILING_ADDRESS = (
    "Stella & Sage Co.\n"
    "1627 Honey Hill Rd\n"
    "El Cajon, CA 92020"
)

SIGNATURE = (
    "Ryan Irwin\n"
    "Founder, Stella & Sage Co.\n"
    "Veteran Owned and Operated\n"
)

OPT_OUT = 'Don\'t want to hear from us? Reply "no thanks" and you won\'t.'


def preview_url(handle: str) -> str:
    return f"{STORE_BASE_URL}/collections/{handle}?preview=1"


def claim_url(handle: str) -> str:
    return f"{STORE_BASE_URL}/pages/join-store?shop={handle}"


def configured() -> bool:
    """Whether this deployment can actually send."""
    return all(
        os.getenv(name, "").strip()
        for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")
    )


def _from_header() -> str:
    """The visible sender.

    SMTP_USER is the login, not the address. With Resend it is the literal
    string "resend", so using it as From — which the original follow-up did,
    back when the login happened to be a Gmail address — produces a message
    from nobody. OUTREACH_FROM_EMAIL is the address; fall back to the login
    only when it at least looks like one.
    """
    configured_from = os.getenv("OUTREACH_FROM_EMAIL", "").strip()
    if configured_from:
        return configured_from
    user = os.getenv("SMTP_USER", "").strip()
    return user if "@" in user else ""


def _reply_to() -> str:
    """Where a human actually reads replies.

    The sending subdomain exists for reputation, not for receiving. Replies
    belong in the mailbox that is already staffed.
    """
    return os.getenv("OUTREACH_REPLY_TO", "").strip()


def _footer() -> str:
    return f"{SIGNATURE}\n{MAILING_ADDRESS}\n\n{OPT_OUT}\n"


def organization_name(state: Dict[str, Any]) -> str:
    """What to call them in the greeting.

    The intake contract carries a storefront name ("Westside Rowing Team
    Store"), and addressing a human with the suffix attached reads like a mail
    merge that nobody checked.
    """
    handle = str(state.get("handle") or "").strip()
    name = str(state.get("storefront_name") or handle).strip()
    trimmed = name
    for suffix in (" Team Store", " Store"):
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)]
            break
    return trimmed.strip() or handle


def _message(*, to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = to
    sender = _from_header()
    if sender:
        message["From"] = sender
    reply_to = _reply_to()
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)
    return message


def first_contact(state: Dict[str, Any]) -> EmailMessage:
    """The opening email.

    Plain text on purpose. A designed template with a tracking pixel is the
    visual signature of bulk mail, and this is one message to one organization
    about a store that already exists for them.
    """
    handle = str(state.get("handle") or "").strip()
    org = organization_name(state)

    body = f"""Hi {org} team,

I run a small veteran-owned apparel company in California. I noticed {org} doesn't have a team store, so I put one together to show you what it could look like — your logo on a tri-blend tee and a hoodie:

{preview_url(handle)}

No sign-in needed. You can click into the admin and change the store's look, colors, and welcome message, or build a product yourself, before deciding anything.

There's no cost and no catch. We make the shirts, we ship them, we handle returns. We make money only if your members actually buy something. If nobody does, I'm out a little time and that's the end of it.

To be clear about the artwork: this is an unofficial concept I built to show you the idea. Your logo is yours. Nothing is public, nothing is for sale, and no one is being charged.

If it looks useful, the first person from {org} to use this link becomes the store's admin, and anyone else who joins with the same link becomes a member:

{claim_url(handle)}

If it's not useful, just reply "no thanks" and I'll delete the whole thing, artwork included. I'll take it down in about a week anyway if I don't hear back.

Happy to answer anything.

{_footer()}"""
    return _message(
        to=str(state.get("contact_email") or "").strip(),
        subject=f"Team store idea for {org} (already built, take a look)",
        body=body,
    )


def follow_up(state: Dict[str, Any]) -> EmailMessage:
    """Day 3. Shorter than the first one — they already have the pitch."""
    handle = str(state.get("handle") or "").strip()
    org = organization_name(state)

    body = f"""Hi {org} team,

Following up once on the team store I built for you:

{preview_url(handle)}

You can look around and even make a product without signing in. If an authorized person wants to keep it, the first one to use this link becomes the admin:

{claim_url(handle)}

If it's not for you, reply "no thanks" and I'll delete it and the artwork.

{_footer()}"""
    return _message(
        to=str(state.get("contact_email") or "").strip(),
        subject=f"Re: Team store idea for {org}",
        body=body,
    )


def send(message: EmailMessage) -> None:
    """Deliver one message. Raises on failure so the caller can record it."""
    if not message["To"]:
        raise ValueError("message has no recipient")
    if not message["From"]:
        raise RuntimeError("OUTREACH_FROM_EMAIL is not configured")
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not (host and user and password):
        raise RuntimeError("SMTP is not configured")
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(message)


def recipient_domain(address: str) -> str:
    """The domain half of an address, for logging without the local part."""
    _name, email = parseaddr(str(address or ""))
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def display_from() -> str:
    """The From header as configured, for a settings readout."""
    sender = _from_header()
    if not sender:
        return ""
    name, email = parseaddr(sender)
    return formataddr((name, email)) if email else sender
