"""Small directory responses; expensive Shopify scans refresh off-request."""
from __future__ import annotations

import copy
import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse

import command_center as cc

_lock = threading.Lock()
_directory_lock = threading.Lock()
_directory = None
_running = False
_last_attempt = 0.0
_last_error = False


def _snapshot():
    with cc._CACHE_LOCK:
        return copy.deepcopy(cc._SUMMARY_CACHE[1]) if cc._SUMMARY_CACHE else None


def _refresh(core, force=False):
    global _running, _last_attempt, _last_error
    with _lock:
        if _running or time.monotonic() - _last_attempt < 30:
            return
        if not force and cc._cache_get() is not None:
            return
        _running = True
        _last_attempt = time.monotonic()

    def build():
        global _running, _last_error
        try:
            with cc._SUMMARY_BUILD_LOCK:
                cc._cache_set(cc._build_summary(core))
            _last_error = False
        except Exception:
            _last_error = True
            core.log.exception("Command Center background refresh failed")
        finally:
            with _lock:
                _running = False
    threading.Thread(target=build, daemon=True, name="command-center-refresh").start()


def _stores(core):
    global _directory
    with _directory_lock:
        if _directory and time.monotonic() - _directory[0] < 60:
            return copy.deepcopy(_directory[1])
        rows = []
        for node in cc._store_nodes(core):
            fields = cc._field_map(node)
            handle = cc._normalized_handle(str(node.get("handle") or ""))
            if handle:
                rows.append({"handle": handle, "name": fields.get("name") or node.get("displayName") or handle,
                             "collection_handle": fields.get("collection_handle") or handle,
                             "status": fields.get("status") or "active",
                             "created_at": fields.get("created_at")})
        _directory = (time.monotonic(), rows)
        return copy.deepcopy(rows)


def _compact(store):
    """Keep ranking/highlights, with no member identities or activity arrays."""
    row = {k: copy.deepcopy(v) for k, v in store.items() if k in {
        "handle", "name", "collection_handle", "status", "created_at", "source",
        "observability", "decision", "products", "sales", "activity", "customers"}}
    row["customers"] = {k: v for k, v in row.get("customers", {}).items() if k not in {"store_admins", "platform_admins"}}
    row.get("sales", {}).pop("recent_purchases", None)
    row.get("activity", {}).pop("recent_sessions", None)
    activity = row.get("activity", {})
    stamps = [
        (activity.get("last_non_super_admin_session") or {}).get("at"),
        (activity.get("last_authenticated_customer_activity") or {}).get("at"),
        (row.get("sales", {}).get("last_purchase") or {}).get("created_at"),
        (row.get("products", {}).get("newest_product") or {}).get("created_at"),
    ]
    row["last_engagement_at"] = max((s for s in stamps if s), default=None)
    return row


def _denied(core, request):
    denied = core._require_admin_secret(request)
    if denied is not None:
        return denied
    if request.headers.get("X-SS-Superadmin") != "1":
        return JSONResponse({"ok": False, "error": "super-admin access required"}, status_code=403)


def install_fast_routes(app, core):
    @app.get("/admin/command-center/index")
    def index(request: Request, refresh: bool = False):
        denied = _denied(core, request)
        if denied is not None:
            return denied
        try:
            directory = _stores(core)
            snapshot = _snapshot()
            _refresh(core, refresh)
            by_handle = {s["handle"]: s for s in (snapshot or {}).get("stores", [])}
            rows = [_compact({**by_handle.get(s["handle"], {}), **s}) for s in directory]
            # Preserve observed collection dates when the directory has no date field.
            for row in rows:
                row["created_at"] = row.get("created_at") or by_handle.get(row["handle"], {}).get("created_at")
            rows.sort(key=lambda s: (s.get("last_engagement_at") or "", s.get("created_at") or ""), reverse=True)
            return {"ok": True, "stores": rows, "highlights": (snapshot or {}).get("highlights", {}),
                    "generated_at": (snapshot or {}).get("generated_at"), "refreshing": _running,
                    "metrics_ready": bool(snapshot), "stale": cc._cache_get() is None,
                    "refresh_failed": _last_error, "data_quality": (snapshot or {}).get("data_quality", {})}
        except Exception:
            core.log.exception("Command Center directory failed")
            return JSONResponse({"ok": False, "error": "Store directory unavailable"}, status_code=502)

    @app.get("/admin/command-center/store/{handle}")
    def detail(handle: str, request: Request):
        denied = _denied(core, request)
        if denied is not None:
            return denied
        if not cc._normalized_handle(handle):
            return JSONResponse({"ok": False, "error": "Invalid store"}, status_code=400)
        snapshot = _snapshot()
        _refresh(core)
        if not snapshot:
            return JSONResponse({"ok": True, "pending": True}, status_code=202)
        row = next((s for s in snapshot["stores"] if s["handle"] == handle), None)
        if not row:
            return JSONResponse({"ok": False, "error": "Store not in snapshot yet", "pending": _running}, status_code=404)
        return {"ok": True, "store": row, "generated_at": snapshot["generated_at"], "stale": cc._cache_get() is None}

    @app.get("/admin/command-center/report")
    def report(request: Request):
        denied = _denied(core, request)
        if denied is not None:
            return denied
        snapshot = _snapshot()
        _refresh(core)
        if not snapshot:
            return JSONResponse({"ok": True, "pending": True}, status_code=202)
        return {"ok": True, "totals": snapshot["totals"], "generated_at": snapshot["generated_at"],
                "data_quality": snapshot["data_quality"], "stale": cc._cache_get() is None}
