"""Server-owned state and APIs for unclaimed prospect-store demos.

The browser never talks to these routes directly.  Printful_Automation's
signed Shopify App Proxy relay calls them with ``X-Admin-Secret`` after it has
verified the proxy signature.  Keeping the one-product allowance in this
service makes the limit durable and independent of browser state.
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any, Dict

from fastapi import Request
from fastapi.responses import JSONResponse

import outreach_tracking


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SAFE_MODEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_INSTALL_LOCK = threading.Lock()
_INSTALLED_APP_IDS: set[int] = set()
_STORE_LOCKS: Dict[str, threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()
_MAX_EVENTS = 100

ALLOWED_EVENTS = {
    "prospect_store_opened",
    "admin_demo_opened",
    "store_customizer_opened",
    "store_appearance_changed",
    "add_product_opened",
    "product_selected",
    "artwork_step_reached",
    "product_preview_completed",
    "demo_product_publish_clicked",
    "authentication_started",
    "store_successfully_claimed",
    "demo_product_successfully_created",
    "demo_product_build_failed",
}


def _store_lock(handle: str) -> threading.Lock:
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(handle)
        if lock is None:
            lock = threading.Lock()
            _STORE_LOCKS[handle] = lock
        return lock


def default_demo_state() -> Dict[str, Any]:
    return {
        "enabled": True,
        "product_limit": 1,
        "product_status": "available",
        "reservation_id": None,
        "product_model": None,
        "product_job_id": None,
        "product_id": None,
        "product_handle": None,
        "reserved_at": None,
        "completed_at": None,
        "event_counts": {},
        "events": [],
    }


def _normalize_handle(raw: Any) -> str:
    handle = str(raw or "").strip().lower()
    return handle if _SAFE_HANDLE.fullmatch(handle) else ""


def _is_unclaimed_prospect(state: Dict[str, Any]) -> bool:
    """Cheap ledger check. Says "maybe" — see _confirm_unclaimed for the answer."""
    return bool(
        state
        and outreach_tracking.is_outreach_source(state.get("source"))
        and str(state.get("store_status") or "").lower() == "prospect_unclaimed"
        and str(state.get("claim_status") or "unclaimed").lower() == "unclaimed"
    )


def _shopify_owner(core: Any, handle: str) -> str:
    """The store's real owner id from Shopify, or "" when still unclaimed."""
    reader = getattr(core, "_fr_get_owner_from_custom_shop", None)
    if not callable(reader):
        return ""
    try:
        return str(reader(handle) or "").strip()
    except Exception as exc:
        print(f"[prospect-demo] owner lookup failed for {handle}: {exc}")
        return ""


def _confirm_unclaimed(core: Any, handle: str, state: Dict[str, Any]) -> bool:
    """Whether this store may still be demoed, with Shopify as the authority.

    claim_status is bookkeeping, and deliberately unreliable: the join route
    grants the claim atomically in Shopify and only then calls mark_claimed,
    inside a try/except that swallows failures so demo bookkeeping can never
    fail a claim that already succeeded. That is the right trade for the claim
    and the wrong one to authorize against — a dropped mark_claimed leaves a
    genuinely claimed store reading "unclaimed" here forever, and the demo is
    what a member invited by the new admin would then be handed.

    So the ledger is only allowed to deny. Granting is checked against the
    custom_shop owner field, which the claim writes before it reports success,
    and a store found already claimed heals its own ledger on the way out.
    """
    if not _is_unclaimed_prospect(state):
        return False
    if not _shopify_owner(core, handle):
        return True

    try:
        state["claim_status"] = "claimed"
        state["store_status"] = "claimed"
        state.setdefault("claimed_at", outreach_tracking.utc_iso())
        outreach_tracking.upsert(core, handle, state)
    except Exception as exc:
        print(f"[prospect-demo] could not heal claim status for {handle}: {exc}")
    return False


def _demo(state: Dict[str, Any]) -> Dict[str, Any]:
    current = state.get("prospect_demo")
    merged = default_demo_state()
    if isinstance(current, dict):
        merged.update(current)
    if not isinstance(merged.get("event_counts"), dict):
        merged["event_counts"] = {}
    if not isinstance(merged.get("events"), list):
        merged["events"] = []
    return merged


def _public_state(
    handle: str,
    state: Dict[str, Any],
    *,
    unclaimed: bool | None = None,
) -> Dict[str, Any]:
    """Pass ``unclaimed`` from _confirm_unclaimed wherever the answer must be
    authoritative. Without it this falls back to the ledger, which can only be
    stale in the permissive direction."""
    demo = _demo(state)
    if unclaimed is None:
        unclaimed = _is_unclaimed_prospect(state)
    return {
        "ok": True,
        "handle": handle,
        "store_status": state.get("store_status") or "",
        "claim_status": state.get("claim_status") or "unclaimed",
        "enabled": unclaimed and bool(demo.get("enabled", True)),
        "product_limit": 1,
        "product_status": demo.get("product_status") or "available",
        "product_model": demo.get("product_model"),
        "product_id": demo.get("product_id"),
        "product_handle": demo.get("product_handle"),
        "completed_at": demo.get("completed_at"),
        "event_counts": dict(demo.get("event_counts") or {}),
    }


# Set by the relay once it has verified the visitor is a super-admin. These
# routes are already behind the admin secret, so nothing but the relay can
# supply it.
STAFF_HEADER = "X-SS-Staff"


def is_staff_request(request: Any) -> bool:
    try:
        return str(request.headers.get(STAFF_HEADER, "")).strip() == "1"
    except Exception:
        return False


def _append_event(
    state: Dict[str, Any],
    event: str,
    *,
    session_id: str = "",
    details: Dict[str, Any] | None = None,
    staff: bool = False,
) -> Dict[str, Any]:
    """Record one thing that happened, and whether it counts.

    Staff events are kept but never counted. Checking your own work is not
    traction, and it must not read as it: the funnel would report interest
    nobody had, and retention would keep a store alive because its owner was
    the one poking at it.
    """
    demo = _demo(state)
    now = outreach_tracking.utc_iso()
    counts = dict(demo.get("event_counts") or {})
    if not staff:
        counts[event] = int(counts.get(event) or 0) + 1
    row: Dict[str, Any] = {"event": event, "at": now}
    if staff:
        row["staff"] = True
    if session_id:
        row["session_id"] = session_id[:80]
    if details:
        row["details"] = {
            str(key)[:50]: str(value)[:200]
            for key, value in details.items()
            if value is not None
        }
    events = list(demo.get("events") or [])
    events.append(row)
    demo["event_counts"] = counts
    demo["events"] = events[-_MAX_EVENTS:]
    if not staff:
        # Retention reads these. A staff visit refreshing one would keep a
        # store alive on the strength of its owner looking at it.
        demo[f"last_{event}_at"] = now
    state["prospect_demo"] = demo
    return row


def record_event(
    core: Any,
    handle: str,
    event: str,
    *,
    session_id: str = "",
    details: Dict[str, Any] | None = None,
    require_unclaimed: bool = True,
    staff: bool = False,
) -> Dict[str, Any]:
    normalized = _normalize_handle(handle)
    if not normalized:
        raise ValueError("invalid store handle")
    if event not in ALLOWED_EVENTS:
        raise ValueError("unsupported prospect demo event")
    with _store_lock(normalized):
        state = outreach_tracking.read(core, normalized)
        if not state:
            raise LookupError("Outreach tracking not found")
        if require_unclaimed and not _confirm_unclaimed(core, normalized, state):
            raise PermissionError("Store is not an unclaimed prospect")
        row = _append_event(
            state,
            event,
            session_id=str(session_id or ""),
            details=details,
            staff=staff,
        )
        outreach_tracking.upsert(core, normalized, state)
        return row


def mark_claimed(core: Any, handle: str, customer_id: str) -> None:
    """Best-effort transition called after the atomic store claim succeeds."""
    normalized = _normalize_handle(handle)
    if not normalized:
        return
    with _store_lock(normalized):
        state = outreach_tracking.read(core, normalized)
        if not state or not outreach_tracking.is_outreach_source(state.get("source")):
            return
        if str(state.get("claim_status") or "").strip().lower() == "claimed":
            return
        state["claim_status"] = "claimed"
        state["store_status"] = "claimed"
        state["claimed_at"] = outreach_tracking.utc_iso()
        state["claimed_customer_id"] = str(customer_id or "")[:80]
        _append_event(
            state,
            "store_successfully_claimed",
            details={"customer_id": str(customer_id or "")[:80]},
        )
        outreach_tracking.upsert(core, normalized, state)


def install_prospect_demo_routes(app: Any, core: Any) -> bool:
    app_id = id(app)
    with _INSTALL_LOCK:
        if app_id in _INSTALLED_APP_IDS:
            return False
        _INSTALLED_APP_IDS.add(app_id)

    def denied(request: Request):
        return core._require_admin_secret(request)

    @app.get("/api/outreach/store/{handle}/demo-state")
    def demo_state(handle: str, request: Request):
        rejection = denied(request)
        if rejection is not None:
            return rejection
        normalized = _normalize_handle(handle)
        if not normalized:
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        state = outreach_tracking.read(core, normalized)
        if not state:
            return JSONResponse({"error": "Outreach tracking not found"}, status_code=404)
        # This response is what unhides Try the admin and what mints the demo
        # token, so the claim check here has to be the authoritative one.
        return _public_state(
            normalized,
            state,
            unclaimed=_confirm_unclaimed(core, normalized, state),
        )

    @app.post("/api/outreach/store/{handle}/demo-event")
    async def demo_event(handle: str, request: Request):
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
        try:
            row = record_event(
                core,
                handle,
                str((body or {}).get("event") or ""),
                session_id=str((body or {}).get("session_id") or ""),
                details=(body or {}).get("details") if isinstance((body or {}).get("details"), dict) else None,
                staff=is_staff_request(request),
            )
            return {"ok": True, "event": row}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/outreach/store/{handle}/demo-product/reserve")
    async def reserve_demo_product(handle: str, request: Request):
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
        normalized = _normalize_handle(handle)
        model = str((body or {}).get("model") or "").strip().lower()
        request_id = str((body or {}).get("request_id") or "").strip()[:120]
        job_id = str((body or {}).get("job_id") or "").strip()[:120]
        if not normalized or not _SAFE_MODEL.fullmatch(model) or not request_id or not job_id:
            return JSONResponse({"error": "handle, model, request_id, and job_id are required"}, status_code=400)

        staff = is_staff_request(request)

        with _store_lock(normalized):
            state = outreach_tracking.read(core, normalized)
            if not state:
                return JSONResponse({"error": "Outreach tracking not found"}, status_code=404)
            if not _confirm_unclaimed(core, normalized, state):
                return JSONResponse({"error": "Store is not an unclaimed prospect"}, status_code=409)
            demo = _demo(state)
            status = str(demo.get("product_status") or "available")
            # Testing the builder must not spend the prospect's one free
            # product. Finding it already used, by someone they never met, is
            # a worse first impression than the demo is a good one.
            if staff and status in {"reserved", "building", "completed"}:
                status = "available"
            if status in {"reserved", "building", "completed"}:
                if status != "completed" and demo.get("request_id") == request_id:
                    return {
                        "ok": True,
                        "status": status,
                        "reservation_id": demo.get("reservation_id"),
                        "idempotent": True,
                    }
                return JSONResponse(
                    {
                        "error": "Want to build more? Claim your free store to unlock unlimited product creation.",
                        "product_status": status,
                    },
                    status_code=409,
                )

            reservation_id = str(uuid.uuid4())
            now = outreach_tracking.utc_iso()
            demo.update({
                "product_status": "reserved",
                "reservation_id": reservation_id,
                "request_id": request_id,
                "product_model": model,
                "product_job_id": job_id,
                "reserved_at": now,
                "product_id": None,
                "product_handle": None,
                "completed_at": None,
            })
            if not staff:
                state["prospect_demo"] = demo
            _append_event(
                state,
                "demo_product_publish_clicked",
                session_id=request_id,
                details={"model": model, "job_id": job_id},
                staff=staff,
            )
            outreach_tracking.upsert(core, normalized, state)
            return {"ok": True, "status": "reserved", "reservation_id": reservation_id}

    @app.post("/api/outreach/store/{handle}/demo-product/complete")
    async def complete_demo_product(handle: str, request: Request):
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
        normalized = _normalize_handle(handle)
        reservation_id = str((body or {}).get("reservation_id") or "").strip()
        product_id = str((body or {}).get("product_id") or "").strip()[:160]
        product_handle = str((body or {}).get("product_handle") or "").strip()[:160]
        if not normalized or not reservation_id or not product_id:
            return JSONResponse({"error": "reservation_id and product_id are required"}, status_code=400)
        with _store_lock(normalized):
            state = outreach_tracking.read(core, normalized)
            if not state:
                return JSONResponse({"error": "Outreach tracking not found"}, status_code=404)
            demo = _demo(state)
            if demo.get("product_status") == "completed" and demo.get("reservation_id") == reservation_id:
                return {"ok": True, "status": "completed", "idempotent": True}
            if demo.get("reservation_id") != reservation_id or demo.get("product_status") not in {"reserved", "building"}:
                return JSONResponse({"error": "Demo product reservation is not active"}, status_code=409)
            now = outreach_tracking.utc_iso()
            demo.update({
                "product_status": "completed",
                "product_id": product_id,
                "product_handle": product_handle or None,
                "completed_at": now,
            })
            state["prospect_demo"] = demo
            _append_event(
                state,
                "demo_product_successfully_created",
                details={"product_id": product_id, "product_handle": product_handle},
            )
            outreach_tracking.upsert(core, normalized, state)
            return {"ok": True, "status": "completed", "product_id": product_id, "product_handle": product_handle}

    @app.post("/api/outreach/store/{handle}/demo-product/fail")
    async def fail_demo_product(handle: str, request: Request):
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
        normalized = _normalize_handle(handle)
        reservation_id = str((body or {}).get("reservation_id") or "").strip()
        if not normalized or not reservation_id:
            return JSONResponse({"error": "reservation_id is required"}, status_code=400)
        with _store_lock(normalized):
            state = outreach_tracking.read(core, normalized)
            if not state:
                return JSONResponse({"error": "Outreach tracking not found"}, status_code=404)
            demo = _demo(state)
            if demo.get("reservation_id") != reservation_id:
                return JSONResponse({"error": "Demo product reservation is not active"}, status_code=409)
            if demo.get("product_status") == "completed":
                return JSONResponse({"error": "Completed demo products cannot be released"}, status_code=409)
            _append_event(
                state,
                "demo_product_build_failed",
                details={"error": str((body or {}).get("error") or "Build failed")[:200]},
            )
            demo = _demo(state)
            demo.update({
                "product_status": "available",
                "reservation_id": None,
                "request_id": None,
                "product_job_id": None,
                "reserved_at": None,
            })
            state["prospect_demo"] = demo
            outreach_tracking.upsert(core, normalized, state)
            return {"ok": True, "status": "available"}

    return True
