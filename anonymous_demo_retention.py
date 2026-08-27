"""Strict 48-hour cleanup for unclaimed website demos.

This is intentionally separate from cold-outreach retention. Outreach keeps an
engaged prospect for review; a website demo always expires 48 hours after it is
ready unless Shopify already has an owner.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List

import outreach_tracking


_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def enabled() -> bool:
    return os.getenv("ANONYMOUS_DEMO_RETENTION_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _eligible(state: Dict[str, Any], now: float) -> bool:
    if not outreach_tracking.is_anonymous_demo_source(state.get("source")):
        return False
    if str(state.get("store_status") or "").lower() != outreach_tracking.ANONYMOUS_DEMO_STORE_STATUS:
        return False
    if str(state.get("claim_status") or "unclaimed").lower() == "claimed":
        return False
    if str(state.get("status") or "").lower() != "ready":
        return False
    due = outreach_tracking.parse_iso(state.get("expires_at"))
    return bool(due and due.timestamp() <= now)


def due_now(core: Any, *, now: float | None = None) -> List[str]:
    moment = time.time() if now is None else now
    try:
        states = outreach_tracking.list_all(core)
    except Exception as exc:
        print(f"[anonymous-demo-retention] ledger read failed: {exc}")
        return []
    return [handle for handle, state in states.items() if _eligible(state, moment)]


def _owner(core: Any, handle: str) -> str | None:
    """Return the normalized owner; None means Shopify could not be checked."""
    try:
        shop = core._get_custom_shop(handle)
    except Exception as exc:
        print(f"[anonymous-demo-retention] owner check failed for {handle}: {exc}")
        return None
    if shop is None:
        return ""
    fields = shop.get("fields") or {}
    raw = str(fields.get("owner_customer_id") or "").strip()
    normalizer = getattr(core, "_normalize_store_owner", None)
    return str(normalizer(raw) if callable(normalizer) else ("" if raw.lower() == "unclaimed" else raw))


def _delete(core: Any, handle: str) -> bool:
    lock_factory = getattr(core, "_store_claim_lock", None)
    lock = lock_factory(handle) if callable(lock_factory) else threading.Lock()
    with lock:
        owner = _owner(core, handle)
        if owner is None:
            return False
        if owner:
            try:
                outreach_tracking.update(core, handle, {
                    "claim_status": "claimed",
                    "store_status": "claimed",
                    "status": "claimed",
                    "expires_at": None,
                    "delete_due_at": None,
                    "claimed_at": outreach_tracking.utc_iso(),
                })
            except Exception as exc:
                print(f"[anonymous-demo-retention] claim heal failed for {handle}: {exc}")
            return False

        job_id = str(uuid.uuid4())
        try:
            core._job_set(job_id, status="queued", handle=handle, created_at=time.time())
            core._run_shopify_deprovision_job(job_id, handle)
            job = core._job_get(job_id) or {}
        except Exception as exc:
            print(f"[anonymous-demo-retention] delete failed for {handle}: {exc}")
            return False
        if str(job.get("status") or "").lower() not in {"done", "succeeded"}:
            print(
                f"[anonymous-demo-retention] {handle} was not deleted: "
                f"{json.dumps(str(job.get('error') or '')[:200])}"
            )
            return False
        try:
            outreach_tracking.update(core, handle, {
                "status": "deleted",
                "store_status": "expired",
                "expired_at": outreach_tracking.utc_iso(),
                "expires_at": None,
                "delete_due_at": None,
                "resume_token_hash": None,
            })
        except Exception as exc:
            print(f"[anonymous-demo-retention] deleted {handle}, ledger update failed: {exc}")
        return True


def process_due(core: Any, *, dry_run: bool = False, now: float | None = None) -> Dict[str, Any]:
    due = due_now(core, now=now)
    deleted = [] if dry_run else [handle for handle in due if _delete(core, handle)]
    return {"ok": True, "dry_run": dry_run, "considered": len(due), "deleted": deleted}


def install_anonymous_demo_retention_scheduler(core: Any, interval_seconds: int = 900) -> bool:
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
                    print(f"[anonymous-demo-retention] deleted {len(result['deleted'])} expired demo(s)")
            except Exception as exc:
                print(f"[anonymous-demo-retention] pass failed: {exc}")

    threading.Thread(target=loop, name="anonymous-demo-retention", daemon=True).start()
    return True

