"""Taking down the stores nobody wanted.

The outreach email says "I'll take it down in about a week anyway if I don't
hear back". Until now nothing did. delete_due_at was written in half a dozen
places and read by none of them, so the one deadline in the pitch was a bluff
and every store ever built was still sitting there.

Only a store nobody touched is deleted. Somebody who opened the admin, changed
the colours or built a product is thinking about it, and deleting their store
on a timer is the rudest possible answer to interest. Those are left alone and
listed for a person instead.

Runs only when OUTREACH_RETENTION_ENABLED is set, so it can deploy before it
is allowed to remove anything.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any, Dict, List

import outreach_tracking


_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

# The events that mean a human looked at this and did something. Opening the
# store is not one of them: an email client prefetching a link would qualify,
# and so would the sender checking their own work.
ENGAGEMENT_EVENTS = (
    "admin_demo_opened",
    "store_customizer_opened",
    "store_appearance_changed",
    "add_product_opened",
    "product_selected",
    "artwork_step_reached",
    "product_preview_completed",
    "demo_product_publish_clicked",
    "demo_product_successfully_created",
    "authentication_started",
)


def enabled() -> bool:
    return os.getenv("OUTREACH_RETENTION_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def engagement(state: Dict[str, Any]) -> List[str]:
    """What the prospect actually did, ignoring anything staff did.

    Staff events are never counted, so a store the founder was testing does
    not look engaged — and, just as importantly, does not get spared on the
    strength of its author poking at it.
    """
    demo = state.get("prospect_demo") if isinstance(state.get("prospect_demo"), dict) else {}
    counts = demo.get("event_counts") if isinstance(demo.get("event_counts"), dict) else {}
    found = []
    for event in ENGAGEMENT_EVENTS:
        try:
            if int(counts.get(event) or 0) > 0:
                found.append(event)
        except (TypeError, ValueError):
            continue
    return found


def _due(state: Dict[str, Any], now: float) -> bool:
    due_at = outreach_tracking.parse_iso(state.get("delete_due_at"))
    return bool(due_at and due_at.timestamp() <= now)


def _eligible(state: Dict[str, Any], now: float) -> bool:
    if not outreach_tracking.is_outreach_source(state.get("source")):
        return False
    if str(state.get("claim_status") or "unclaimed").lower() == "claimed":
        return False
    if str(state.get("status") or "").lower() in {"declined", "intake_failed", "failed"}:
        return False
    if not state.get("sent_at"):
        # Never emailed, so the week the email promised never started.
        return False
    return _due(state, now)


def due_now(core: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Split the overdue stores into the ones to delete and the ones to keep."""
    delete: List[Dict[str, Any]] = []
    keep: List[Dict[str, Any]] = []
    now = time.time()
    try:
        ledger = outreach_tracking.list_all(core)
    except Exception as exc:
        print(f"[outreach-retention] could not read the ledger: {exc}")
        return {"delete": delete, "keep": keep}

    for handle, state in ledger.items():
        if not _eligible(state, now):
            continue
        row = {
            "handle": handle,
            "sent_at": state.get("sent_at"),
            "delete_due_at": state.get("delete_due_at"),
            "engagement": engagement(state),
        }
        (keep if row["engagement"] else delete).append(row)
    return {"delete": delete, "keep": keep}


def _delete(core: Any, handle: str, engagement_seen: List[str]) -> bool:
    try:
        outreach_tracking.update(core, handle, {
            "status": "declined",
            "declined_at": outreach_tracking.utc_iso(),
            "review_decision": "expired",
            "review_note": "No reply and no activity within the window the email promised.",
            "email_authorized": False,
            "followup_due_at": None,
            "delete_due_at": None,
        })
        job_id = str(uuid.uuid4())
        core._job_set(job_id, status="queued", handle=handle, created_at=time.time())
        threading.Thread(
            target=core._run_shopify_deprovision_job,
            args=(job_id, handle),
            name=f"outreach-expire-{handle}",
            daemon=True,
        ).start()
        return True
    except Exception as exc:
        print(f"[outreach-retention] could not delete {handle}: {exc}")
        return False


def process_due(core: Any, *, dry_run: bool = False) -> Dict[str, Any]:
    """Delete the untouched, keep the interested. Returns what it did."""
    split = due_now(core)
    deleted: List[str] = []

    if not dry_run:
        for row in split["delete"]:
            if _delete(core, row["handle"], row["engagement"]):
                deleted.append(row["handle"])

    kept = [row["handle"] for row in split["keep"]]
    if kept:
        # Worth a person's attention: they looked, they did something, and
        # they still have not claimed.
        print(f"[outreach-retention] kept {len(kept)} store(s) with activity: {', '.join(kept)}")
    return {
        "ok": True,
        "dry_run": dry_run,
        "deleted": deleted,
        "kept": kept,
        "considered": len(split["delete"]) + len(split["keep"]),
    }


def install_outreach_retention_scheduler(core: Any, interval_seconds: int = 3600) -> bool:
    """Check hourly. Inert unless OUTREACH_RETENTION_ENABLED is set."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        _INSTALLED = True

    def loop() -> None:
        while True:
            time.sleep(interval_seconds)
            if not enabled():
                continue
            try:
                result = process_due(core)
                if result["deleted"]:
                    print(f"[outreach-retention] deleted {len(result['deleted'])} untouched store(s)")
            except Exception as exc:
                print(f"[outreach-retention] pass failed: {exc}")

    threading.Thread(target=loop, name="outreach-retention", daemon=True).start()
    return True
