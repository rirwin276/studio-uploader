"""Optional SMTP follow-up scheduler for provisioned outreach stores."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict

import outreach_mail
import outreach_tracking


_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def process_due_followups(core: Any) -> int:
    """Send authorized, due follow-ups; leave them pending when SMTP is unset."""
    if not outreach_mail.configured():
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
        message = outreach_mail.follow_up(state)
        try:
            outreach_mail.send(message)
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
