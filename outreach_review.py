"""The morning review queue: one page, one decision per store.

Discovery and building run unattended overnight. Building costs nothing — no
one is charged until an order exists — so stores are built first and the only
thing waiting on a human is whether a stranger gets an email. That is the
decision worth a person's judgment, and it is one tap.

Accept sends the first-contact email and starts the follow-up clock. Decline
deletes the store and its artwork and emails nobody. Neither happens on a timer;
this module never acts on its own.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any, Dict, List

from fastapi import Request
from fastapi.responses import JSONResponse

import outreach_mail
import outreach_mockups
import outreach_tracking
from outreach_auth import require_outreach_secret


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_INSTALL_LOCK = threading.Lock()
_INSTALLED_APP_IDS: set[int] = set()

# Built and waiting on a decision. Anything already sent, declined or failed has
# left the queue.
PENDING_STATUSES = {"provisioned"}

REVIEW_REASON_LABELS = {
    "existing_merch": "Already sells merchandise",
    "too_large": "Organization is too large or established",
    "bad_logo": "Logo or artwork is unusable/inaccurate",
    "not_team": "Not a small active team or competition club",
    "wrong_contact": "Wrong or unusable contact",
    "duplicate": "Duplicate organization",
    "other": "Other",
}


def _review_reason(code: str = "", note: str = "") -> tuple[str, str]:
    """Normalize a button choice while preserving old free-form clients."""
    normalized = str(code or "").strip().lower()
    text = str(note or "").strip()[:300]
    if normalized not in REVIEW_REASON_LABELS:
        lower = text.lower()
        if any(word in lower for word in ("merch", "shop", "store already", "already sell")):
            normalized = "existing_merch"
        elif any(word in lower for word in ("too big", "too large", "established", "large program")):
            normalized = "too_large"
        elif any(word in lower for word in ("logo", "artwork", "redraw", "image")):
            normalized = "bad_logo"
        elif any(word in lower for word in ("not a team", "not a club", "wrong audience")):
            normalized = "not_team"
        elif any(word in lower for word in ("email", "contact")):
            normalized = "wrong_contact"
        elif "duplicate" in lower:
            normalized = "duplicate"
        else:
            normalized = "other"
    return normalized, text or REVIEW_REASON_LABELS[normalized]


def _pending(state: Dict[str, Any]) -> bool:
    if str(state.get("status") or "").strip().lower() not in PENDING_STATUSES:
        return False
    if state.get("sent_at") or state.get("declined_at"):
        return False
    return outreach_tracking.is_outreach_source(state.get("source"))


def _row(handle: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """What the reviewer needs to decide, and nothing else."""
    return {
        "handle": handle,
        "organization": outreach_mail.organization_name(state),
        "storefront_name": state.get("storefront_name") or handle,
        "type_of_store": state.get("type_of_store") or "",
        "primary_color": state.get("primary_color") or "",
        "contact_email": state.get("contact_email") or "",
        "organization_url": state.get("organization_url") or "",
        "contact_source_url": state.get("contact_source_url") or "",
        "logo_source_url": state.get("logo_source_url") or "",
        "source_agent": state.get("source_agent") or "",
        # A redrawn logo is the one thing worth looking at the preview for
        # before emailing a stranger about it.
        "logo_recreated": bool(state.get("logo_recreated")),
        "logo_quality": state.get("logo_quality") or "",
        "logo_quality_reason": state.get("logo_quality_reason") or "",
        "built_at": state.get("built_at") or state.get("created_at"),
        "preview_url": outreach_mail.preview_url(handle),
        "claim_url": outreach_mail.claim_url(handle),
    }


def pending_queue(core: Any) -> List[Dict[str, Any]]:
    rows = [
        _row(handle, state)
        for handle, state in outreach_tracking.list_all(core).items()
        if _pending(state)
    ]
    rows.sort(key=lambda row: str(row.get("built_at") or ""), reverse=True)
    return rows


def accept(core: Any, handle: str) -> Dict[str, Any]:
    """Send the first-contact email and start the clock.

    The clock is started only after the send succeeds. Stamping the dates first
    would leave a store that was never contacted counting down toward a
    follow-up about an email nobody received, and toward deletion for not
    replying to it.
    """
    state = outreach_tracking.read(core, handle)
    if not state:
        raise LookupError("Outreach tracking not found")
    if not _pending(state):
        raise PermissionError("Store is not waiting for a decision")
    if not str(state.get("contact_email") or "").strip():
        raise ValueError("Store has no contact email")
    if not outreach_mail.configured():
        raise RuntimeError("SMTP is not configured")

    photos = outreach_mockups.for_email(core, handle)
    outreach_mail.send(outreach_mail.first_contact(state, photos))

    sent_at = outreach_tracking.utc_iso()
    patch = {
        "sent_at": sent_at,
        "followup_due_at": outreach_tracking.add_days_iso(sent_at, 3),
        "delete_due_at": outreach_tracking.add_days_iso(sent_at, 7),
        "email_authorized": True,
        "status": "outreach_sent",
        "reviewed_at": sent_at,
        "review_decision": "accepted",
    }
    updated = outreach_tracking.update(core, handle, patch)
    return {
        "ok": True,
        "handle": handle,
        "status": "outreach_sent",
        "sent_at": updated.get("sent_at"),
        "followup_due_at": updated.get("followup_due_at"),
        "delete_due_at": updated.get("delete_due_at"),
    }


FAILED_STATUSES = {"intake_failed", "failed"}


def retry(core: Any, handle: str) -> Dict[str, Any]:
    """Put a failed store back in the queue.

    A failure is otherwise permanent in both directions: the worker only picks
    up queued work, so nothing retries it, and discovery avoids every domain
    already in the ledger, so nothing rediscovers it either. An organization
    that failed for a reason since fixed — a logo too small for the old
    pipeline, say — would never be reached again without this.
    """
    state = outreach_tracking.read(core, handle)
    if not state:
        raise LookupError("Outreach tracking not found")
    if not outreach_tracking.is_outreach_source(state.get("source")):
        raise PermissionError("Not an outreach store")
    status = str(state.get("status") or "").strip().lower()
    if status not in FAILED_STATUSES:
        # Requeuing a live store would build a second one over the top of it.
        raise PermissionError(f"Store is not in a failed state (it is {status or 'unknown'})")

    updated = outreach_tracking.update(
        core,
        handle,
        {
            "status": "intake_queued",
            "intake_error": None,
            "failed_at": None,
            "requeued_at": outreach_tracking.utc_iso(),
        },
    )
    return {
        "ok": True,
        "handle": handle,
        "status": "intake_queued",
        "attempts": updated.get("intake_attempt_count") or 0,
    }


def remove(
    core: Any,
    handle: str,
    reason: str = "",
    reason_code: str = "",
) -> Dict[str, Any]:
    """Delete an outreach store at any stage. The nuke, from the outreach page.

    Decline is the reviewer's "no" and only applies to a store still waiting
    for a decision. This is the other thing: a store that is simply wrong —
    built from the wrong organization's logo, say — and should not exist,
    whether it was already emailed or never left the queue.

    A claimed store is refused. That one belongs to somebody now, and deleting
    it would take a real customer's storefront out from under them.
    """
    state = outreach_tracking.read(core, handle)
    if not state:
        raise LookupError("Outreach tracking not found")
    if not outreach_tracking.is_outreach_source(state.get("source")):
        raise PermissionError("Not an outreach store")
    if str(state.get("claim_status") or "unclaimed").lower() == "claimed":
        raise PermissionError("Store has been claimed — it belongs to its admin now")

    removed_at = outreach_tracking.utc_iso()
    normalized_code, normalized_note = _review_reason(reason_code, reason)
    outreach_tracking.update(
        core,
        handle,
        {
            "status": "declined",
            "declined_at": removed_at,
            "reviewed_at": removed_at,
            "review_decision": "removed",
            "review_reason_code": normalized_code,
            "review_note": normalized_note,
            "email_authorized": False,
            "followup_due_at": None,
            "delete_due_at": None,
            "needs_human_reply": False,
        },
    )

    job_id = str(uuid.uuid4())
    core._job_set(job_id, status="queued", handle=handle, created_at=time.time())
    threading.Thread(
        target=core._run_shopify_deprovision_job,
        args=(job_id, handle),
        name=f"outreach-remove-{handle}",
        daemon=True,
    ).start()
    return {"ok": True, "handle": handle, "status": "declined", "job_id": job_id}


def decline(
    core: Any,
    handle: str,
    reason: str = "",
    reason_code: str = "",
) -> Dict[str, Any]:
    """Delete the store and the artwork. Nobody is emailed.

    Recorded before the deletion starts, because a nuke that half-finishes must
    not leave the store looking like it is still waiting for a decision.
    """
    state = outreach_tracking.read(core, handle)
    if not state:
        raise LookupError("Outreach tracking not found")
    if not _pending(state):
        raise PermissionError("Store is not waiting for a decision")

    declined_at = outreach_tracking.utc_iso()
    normalized_code, normalized_note = _review_reason(reason_code, reason)
    outreach_tracking.update(
        core,
        handle,
        {
            "declined_at": declined_at,
            "reviewed_at": declined_at,
            "review_decision": "declined",
            "review_reason_code": normalized_code,
            "review_note": normalized_note,
            "status": "declined",
            "email_authorized": False,
            "followup_due_at": None,
            "delete_due_at": None,
        },
    )

    job_id = str(uuid.uuid4())
    core._job_set(job_id, status="queued", handle=handle, created_at=time.time())
    threading.Thread(
        target=core._run_shopify_deprovision_job,
        args=(job_id, handle),
        name=f"outreach-decline-{handle}",
        daemon=True,
    ).start()
    return {"ok": True, "handle": handle, "status": "declined", "job_id": job_id}


def _activity(state: Dict[str, Any]) -> Dict[str, Any]:
    """What the prospect actually did, from the demo ledger."""
    demo = state.get("prospect_demo") if isinstance(state.get("prospect_demo"), dict) else {}
    counts = demo.get("event_counts") if isinstance(demo.get("event_counts"), dict) else {}

    def seen(name: str) -> bool:
        try:
            return int(counts.get(name) or 0) > 0
        except (TypeError, ValueError):
            return False

    return {
        "opened_store": seen("prospect_store_opened"),
        "opened_admin": seen("admin_demo_opened"),
        "changed_look": seen("store_appearance_changed"),
        "built_product": seen("demo_product_successfully_created"),
        "started_claim": seen("authentication_started"),
        "product_status": demo.get("product_status") or "available",
    }


def _steps(state: Dict[str, Any], job: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """The trail a store left on its way through, as timestamps not prose.

    The ledger already records each transition. Showing them in order answers
    "is this stuck or moving" without any new instrumentation, and the gap
    between two of them says where the time went.
    """
    status = str(state.get("status") or "").strip().lower()
    trail = [
        ("Queued", state.get("created_at"), True),
        ("Logo prepared, build started", state.get("build_started_at"), True),
        ("Store and products built", state.get("built_at"), True),
    ]
    if status in {"intake_failed", "failed"}:
        trail.append(("Stopped", state.get("failed_at"), True))
    elif state.get("sent_at"):
        trail.append(("Emailed", state.get("sent_at"), True))

    rows = []
    for label, at, _always in trail:
        rows.append({"label": label, "at": at, "done": bool(at)})
    if job:
        rows.append({
            "label": "Provisioner: " + str(job.get("status") or "unknown"),
            "at": None,
            "done": str(job.get("status") or "") == "succeeded",
        })
    return rows


def _job(core: Any, state: Dict[str, Any]) -> Dict[str, Any]:
    """The provisioning job, when there is one to look at."""
    job_id = str(state.get("job_id") or "").strip()
    getter = getattr(core, "_job_get", None)
    if not job_id or not callable(getter):
        return {}
    try:
        return getter(job_id) or {}
    except Exception:
        return {}


def _log_tail(job: Dict[str, Any], lines: int = 12) -> List[str]:
    """The end of the provisioner's own output.

    Only the tail: the full log is hundreds of lines of Shopify and Printful
    chatter, and the part that explains a failure is always at the bottom.
    """
    text = str(job.get("stderr") or "") or str(job.get("stdout") or "")
    if not text.strip():
        return []
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return kept[-lines:]


def _stage(state: Dict[str, Any]) -> str:
    """Where this store sits in the funnel."""
    status = str(state.get("status") or "").strip().lower()
    if str(state.get("claim_status") or "").strip().lower() in {"claimed", "active"}:
        return "claimed"
    if state.get("declined_at") or status == "declined":
        return "declined"
    if status in {"intake_failed", "failed"}:
        return "failed"
    if state.get("sent_at"):
        return "sent"
    if status in PENDING_STATUSES:
        return "pending"
    return "building"


def _pipeline_row(core: Any, handle: str, state: Dict[str, Any]) -> Dict[str, Any]:
    stage = _stage(state)
    # The provisioner is only worth asking about while a store is still moving
    # or has stopped. A sent or claimed store's job finished days ago.
    job = _job(core, state) if stage in {"building", "failed"} else {}
    row = _row(handle, state)
    row.update({
        "stage": stage,
        "error": state.get("intake_error") or state.get("build_error") or "",
        "steps": _steps(state, job),
        "log": _log_tail(job),
        "status": state.get("status") or "",
        "sent_at": state.get("sent_at"),
        "followup_sent_at": state.get("followup_sent_at"),
        "followup_due_at": state.get("followup_due_at"),
        "delete_due_at": state.get("delete_due_at"),
        "claimed_at": state.get("claimed_at"),
        "activity": _activity(state),
    })
    return row


def pipeline(core: Any) -> Dict[str, Any]:
    """Every outreach store, grouped by where it is in the funnel.

    One read of the ledger answers the whole page. Splitting this across a
    request per stage would multiply Shopify calls for a view whose entire
    point is seeing the stages next to each other.
    """
    rows: List[Dict[str, Any]] = []
    for handle, state in outreach_tracking.list_all(core).items():
        if not outreach_tracking.is_outreach_source(state.get("source")):
            continue
        rows.append(_pipeline_row(core, handle, state))

    def when(row: Dict[str, Any]) -> str:
        return str(row.get("sent_at") or row.get("built_at") or "")

    stages: Dict[str, List[Dict[str, Any]]] = {
        name: [] for name in ("pending", "sent", "claimed", "building", "failed", "declined")
    }
    for row in rows:
        stages.setdefault(row["stage"], []).append(row)
    for name in stages:
        stages[name].sort(key=when, reverse=True)

    live = [row for row in rows if row["stage"] != "declined"]
    return {
        "totals": {
            "found": len(rows),
            "built": sum(1 for row in live if row["stage"] in {"pending", "sent", "claimed"}),
            "emailed": sum(1 for row in live if row.get("sent_at")),
            "visited": sum(1 for row in live if row["activity"]["opened_store"]),
            "tried_admin": sum(1 for row in live if row["activity"]["opened_admin"]),
            "made_product": sum(1 for row in live if row["activity"]["built_product"]),
            "claimed": len(stages["claimed"]),
            "declined": len(stages["declined"]),
        },
        "pending": stages["pending"],
        "sent": stages["sent"],
        "claimed": stages["claimed"],
        "building": stages["building"],
        "failed": stages["failed"],
    }


def install_outreach_review_routes(app: Any, core: Any) -> bool:
    app_id = id(app)
    with _INSTALL_LOCK:
        if app_id in _INSTALLED_APP_IDS:
            return False
        _INSTALLED_APP_IDS.add(app_id)

    def _handle(raw: str) -> str:
        handle = str(raw or "").strip().lower()
        return handle if _SAFE_HANDLE.fullmatch(handle) else ""

    @app.get("/api/outreach/review/queue")
    def review_queue(request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        return {
            "ok": True,
            "email_configured": outreach_mail.configured(),
            "from": outreach_mail.display_from(),
            "decline_reasons": REVIEW_REASON_LABELS,
            "pending": pending_queue(core),
        }

    @app.get("/api/outreach/review/pipeline")
    def review_pipeline(request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        return {
            "ok": True,
            "email_configured": outreach_mail.configured(),
            "from": outreach_mail.display_from(),
            "decline_reasons": REVIEW_REASON_LABELS,
            **pipeline(core),
        }

    @app.post("/api/outreach/review/{handle}/accept")
    def review_accept(handle: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        normalized = _handle(handle)
        if not normalized:
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        try:
            return accept(core, normalized)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            # The send failed and no dates were written, so the store is still
            # in the queue and the button can simply be pressed again.
            print(f"[outreach-review] accept failed for {normalized}: {exc}")
            return JSONResponse(
                {"error": f"Could not send the email: {exc}"},
                status_code=502,
            )

    @app.post("/api/outreach/review/{handle}/remove")
    async def review_remove(handle: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        normalized = _handle(handle)
        if not normalized:
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return remove(
                core,
                normalized,
                str((body or {}).get("reason") or ""),
                str((body or {}).get("reason_code") or ""),
            )
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/outreach/review/{handle}/retry")
    def review_retry(handle: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        normalized = _handle(handle)
        if not normalized:
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        try:
            return retry(core, normalized)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/outreach/review/{handle}/decline")
    async def review_decline(handle: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        normalized = _handle(handle)
        if not normalized:
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return decline(
                core,
                normalized,
                str((body or {}).get("reason") or ""),
                str((body or {}).get("reason_code") or ""),
            )
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    return True
