"""Composing and sending outreach mail.

One module owns the envelope, the signature and the legal footer, because the
alternative is three modules each getting some of it right. Nothing here sends
on a schedule; callers decide when.

Sending is off unless SMTP_HOST, SMTP_USER and SMTP_PASS are all set. That is
deliberate: an unconfigured deployment must no-op rather than raise, so the
review queue can be exercised long before mail is live.
"""

from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr, make_msgid
from typing import Any, Dict, List, Sequence, Tuple


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


# A message is written once as blocks and rendered twice. Writing the plain
# text and the HTML separately means every future edit to the pitch has to be
# made in both, and the one that gets forgotten is the one somebody reads.
Block = Tuple[str, str]

# Deliberately close to no styling at all. A designed template with a logo bar
# and a call-to-action button is the visual signature of bulk mail; this should
# read as a person who wrote an email and attached two photos.
_HTML_WRAP = (
    '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
    'Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;'
    'color:#1c1c1e;max-width:560px;">{body}</div>'
)


def _render_text(blocks: Sequence[Block]) -> str:
    out: List[str] = []
    for kind, value in blocks:
        # A photos block carries the line that stands in for the pictures when
        # there are none to show — a reader in plain text gets told they were
        # attached rather than a sentence pointing at nothing.
        if not value:
            continue
        out.append(value)
    return "\n\n".join(out).strip() + "\n"


def _render_html(blocks: Sequence[Block], photo_cids: Sequence[str]) -> str:
    out: List[str] = []
    for kind, value in blocks:
        if kind == "url":
            safe = html.escape(value, quote=True)
            out.append(f'<p style="margin:0 0 14px;"><a href="{safe}">{safe}</a></p>')
        elif kind == "photos":
            for cid in photo_cids:
                # cid: references the attached part, so the picture shows even
                # when a client blocks remote images — which, for a first email
                # from an unknown sender, most of them do.
                out.append(
                    f'<p style="margin:0 0 14px;"><img src="cid:{cid}" '
                    'alt="Your logo on one of the garments" '
                    'style="width:100%;max-width:320px;height:auto;'
                    'border-radius:8px;border:1px solid #e5e5e7;"></p>'
                )
        elif kind == "sig":
            safe = html.escape(value).replace("\n", "<br>")
            out.append(
                f'<p style="margin:18px 0 0;color:#6e6e73;font-size:13px;'
                f'line-height:1.5;">{safe}</p>'
            )
        else:
            safe = html.escape(value).replace("\n", "<br>")
            out.append(f'<p style="margin:0 0 14px;">{safe}</p>')
    return _HTML_WRAP.format(body="".join(out))


def _compose(
    *,
    to: str,
    subject: str,
    blocks: Sequence[Block],
    photos: Sequence[Dict[str, Any]] = (),
) -> EmailMessage:
    """Build the message, with the photos inline when there are any.

    Without photos this stays a plain-text email, exactly as before. Adding an
    empty HTML alternative would make every message multipart for no gain.
    """
    message = _message(to=to, subject=subject, body=_render_text(blocks))
    if not photos:
        return message

    cids = [make_msgid()[1:-1] for _ in photos]
    message.add_alternative(_render_html(blocks, cids), subtype="html")
    related = message.get_payload()[-1]
    for photo, cid in zip(photos, cids):
        related.add_related(
            photo["data"],
            maintype="image",
            subtype=str(photo.get("subtype") or "jpeg"),
            cid=f"<{cid}>",
            filename=_photo_filename(photo),
            disposition="inline",
        )
    return message


def _photo_filename(photo: Dict[str, Any]) -> str:
    """A name a person would recognise if their client lists attachments."""
    title = str(photo.get("title") or "mockup").strip() or "mockup"
    safe = "".join(char if char.isalnum() or char in " -_" else "" for char in title)
    extension = str(photo.get("subtype") or "jpeg").lower()
    if extension == "jpeg":
        extension = "jpg"
    return f"{safe.strip()[:60] or 'mockup'}.{extension}"


def first_contact(
    state: Dict[str, Any], photos: Sequence[Dict[str, Any]] = ()
) -> EmailMessage:
    """The opening email.

    No template and no tracking pixel. The only pictures are the prospect's own
    logo on two garments, which is the part of this that does the persuading —
    a link asks somebody to imagine it, a photo does the imagining for them.
    """
    handle = str(state.get("handle") or "").strip()
    org = organization_name(state)

    blocks: List[Block] = [
        ("p", f"Hi {org} team,"),
        ("p", f"I run a small veteran-owned apparel company in California. I noticed "
              f"{org} doesn't have a team store, so I put one together to show you "
              f"what it could look like — your logo on a tri-blend tee and a hoodie:"),
    ]
    if photos:
        blocks.append(
            ("photos", "(I've attached the two photos so you can see them without "
                       "clicking anything.)")
        )
    # Said plainly, near the top, in our own words. A prospect who is told we
    # redrew their mark finds it thorough; one who works it out for themselves
    # finds it something else entirely.
    if state.get("logo_recreated"):
        blocks.append(
            ("p", "One thing to flag: the logo I could find for you online was too "
                  "low-resolution to print from, so the version on these is one I "
                  "redrew at print quality. It should be a faithful copy, but it is "
                  "my redraw and not your file. Send me your original artwork and "
                  "I'll swap it in myself, or you can replace it in a couple of "
                  "clicks once the store is yours.")
        )
    blocks += [
        ("p", "The whole store is here:"),
        ("url", preview_url(handle)),
        ("p", "No sign-in needed. You can click into the admin and change the store's "
              "look, colors, and welcome message, or build a product yourself, before "
              "deciding anything."),
        ("p", "There's no cost and no catch. We make the shirts, we ship them, we "
              "handle returns. We make money only if your members actually buy "
              "something. If nobody does, I'm out a little time and that's the end of it."),
        ("p", "To be clear about the artwork: this is an unofficial concept I built to "
              "show you the idea. Your logo is yours. Nothing is public, nothing is for "
              "sale, and no one is being charged."),
        ("p", f"If it looks useful, the first person from {org} to use this link becomes "
              f"the store's admin, and anyone else who joins with the same link becomes "
              f"a member:"),
        ("url", claim_url(handle)),
        ("p", 'If it\'s not useful, just reply "no thanks" and I\'ll delete the whole '
              "thing, artwork included. I'll take it down in about a week anyway if I "
              "don't hear back."),
        ("p", "Happy to answer anything."),
        ("sig", _footer().rstrip("\n")),
    ]
    return _compose(
        to=str(state.get("contact_email") or "").strip(),
        subject=f"Team store idea for {org} (already built, take a look)",
        blocks=blocks,
        photos=photos,
    )


def follow_up(
    state: Dict[str, Any], photos: Sequence[Dict[str, Any]] = ()
) -> EmailMessage:
    """Day 3. Shorter than the first one — they already have the pitch.

    One photo, not two. The pitch is the part they have already read; the
    garment is the part worth showing again.
    """
    handle = str(state.get("handle") or "").strip()
    org = organization_name(state)
    photos = list(photos)[:1]

    blocks: List[Block] = [
        ("p", f"Hi {org} team,"),
        ("p", "Following up once on the team store I built for you:"),
    ]
    if photos:
        blocks.append(("photos", "(Photo attached.)"))
    blocks += [
        ("url", preview_url(handle)),
        ("p", "You can look around and even make a product without signing in. If an "
              "authorized person wants to keep it, the first one to use this link "
              "becomes the admin:"),
        ("url", claim_url(handle)),
        ("p", 'If it\'s not for you, reply "no thanks" and I\'ll delete it and the artwork.'),
        ("sig", _footer().rstrip("\n")),
    ]
    return _compose(
        to=str(state.get("contact_email") or "").strip(),
        subject=f"Re: Team store idea for {org}",
        blocks=blocks,
        photos=photos,
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
