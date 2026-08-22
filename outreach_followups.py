"""Optional SMTP follow-up scheduler for provisioned outreach stores."""

from __future__ import annotations

import os
import smtplib
import threading
import time
from email.message import EmailMessage
from typing import Any, Dict

import outreach_tracking


_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def _followup_message(state: Dict[str, Any]) -> EmailMessage:
    handle = str(state.get("handle") or "").strip()
    name = str(state.get("storefront_name") or handle).strip()
    preview = f"https://stellasageco.com/collections/{handle}?preview=1"
    claim = f"https://stellasageco.com/pages/join-store?shop={handle}"
    message = EmailMessage()
    message["Subject"] = f"Quick follow-up: {name} private store"
    message["To"] = str(state.get("contact_email") or "").strip()
    message["From"] = os.getenv("SMTP_USER", "").strip()
    message.set_content(
        f"""Hello {name} team,

Just a quick follow-up on the private, unofficial {name} spirit-wear concept:
{preview}

If an authorized person claims the store, you can easily edit shirt and hoodie colors, move or resize the logo, add artwork, and add products from the admin dashboard. The first verified account to use the private claim link becomes the store administrator:
{claim}

If it is not useful, reply “no” and I will remove the private concept and artwork.

Ryan Irwin
Founder, Stella & Sage Co.
Veteran Owned and Operated
"""
    )
    return message


def process_due_followups(core: Any) -> int:
    """Send authorized, due follow-ups; leave them pending when SMTP is unset."""
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not host or not user or not password:
        return 0

    now = time.time()
    sent = 0
    for handle, state in outreach_tracking.list_all(core).items():
        due = outreach_tracking.parse_iso(state.get("followup_due_at"))
        recipient = str(state.get("contact_email") or "").strip()
        claimed = str(state.get("claim_status") or "unclaimed").strip().lower() in {
            "claimed",
            "active",
        }
        if (
            claimed
            or state.get("email_authorized", True) is not True
            or not due
            or due.timestamp() > now
            or state.get("followup_sent_at")
            or not recipient
        ):
            continue
        message = _followup_message(state)
        try:
            with smtplib.SMTP(
                host,
                int(os.getenv("SMTP_PORT", "587")),
                timeout=20,
            ) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(message)
            outreach_tracking.update(
                core,
                handle,
                {
                    "followup_sent_at": outreach_tracking.utc_iso(),
                    "status": "followup_sent",
                },
            )
            sent += 1
        except Exception as exc:
            print(f"⚠️ follow-up failed for {handle}: {exc}")
    return sent


def install_outreach_followup_scheduler(
    core: Any,
    *,
    delay_seconds: float = 3.0,
) -> bool:
    """Start only the optional follow-up scheduler, never a build/delete worker."""
    global _INSTALLED
    enabled = os.getenv(
        "OUTREACH_FOLLOWUP_SCHEDULER_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "y"}
    if not enabled:
        return False
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        _INSTALLED = True

    def worker() -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        interval = max(300, int(os.getenv("OUTREACH_FOLLOWUP_INTERVAL_S", "900")))
        while True:
            try:
                process_due_followups(core)
            except Exception as exc:
                print(f"⚠️ outreach follow-up scan failed: {exc}")
            time.sleep(interval)

    threading.Thread(
        target=worker,
        name="outreach-followup-scheduler",
        daemon=True,
    ).start()
    return True
