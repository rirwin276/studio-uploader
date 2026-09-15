"""Bounded first-party journeys persisted separately from live store records.

No IPs, query strings, form values, email addresses or raw session IDs are stored.
Bots are filtered heuristically, never asserted to be perfectly identifiable.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import command_center as cc
from command_center_fast import _denied

TYPE = "ss_site_session"
LIMIT = 1000
_locks = [threading.Lock() for _ in range(64)]
_definition_lock = threading.Lock()
_definition_ready = False
_list_lock = threading.Lock()
_list_cache = None
BOT = re.compile(r"bot\b|crawler|spider|headless|lighthouse|pagespeed|facebookexternalhit|meta-external|preview|slurp|wget|curl|python-requests|httpx", re.I)
EVENTS = {"page_view", "heartbeat", "create_opened", "create_started", "create_submitted", "create_accepted", "create_failed", "create_validation_failed", "sign_in_clicked"}


def _path(raw):
    value = str(raw or "")[:1024]
    parsed = urlsplit(value)
    path = parsed.path
    if parsed.netloc or not path.startswith("/") or path.startswith("//"):
        return "/"
    # Avoid token-bearing account / checkout URLs and cap retained path lengths.
    if path.startswith(("/account", "/authentication", "/checkouts", "/checkout")):
        return "/account" if path.startswith(("/account", "/authentication")) else "/checkout"
    return path[:200]


def _ensure(core):
    global _definition_ready
    with _definition_lock:
        if _definition_ready:
            return
        result = core._shopify_graphql("query($type:String!){metaobjectDefinitionByType(type:$type){id}}", {"type": TYPE})
        if not result.get("metaobjectDefinitionByType"):
            result = core._shopify_graphql("""mutation($definition:MetaobjectDefinitionCreateInput!){
              metaobjectDefinitionCreate(definition:$definition){metaobjectDefinition{id} userErrors{message}}}""", {
                "definition": {"type": TYPE, "name": "Site session", "fieldDefinitions": [
                    {"key": "data", "name": "Data", "type": "json"}]}})
            if result.get("metaobjectDefinitionCreate", {}).get("userErrors"):
                raise RuntimeError("Session definition unavailable")
        _definition_ready = True


def _read(core, key):
    result = core._shopify_graphql("""query($handle:MetaobjectHandleInput!){
      metaobjectByHandle(handle:$handle){fields{key value}}}""", {"handle": {"type": TYPE, "handle": key}})
    raw = cc._field_map(result.get("metaobjectByHandle") or {}).get("data")
    return json.loads(raw) if raw else {}


def _write(core, key, row):
    result = core._shopify_graphql("""mutation($handle:MetaobjectHandleInput!,$metaobject:MetaobjectUpsertInput!){
      metaobjectUpsert(handle:$handle,metaobject:$metaobject){metaobject{id} userErrors{message}}}""", {
        "handle": {"type": TYPE, "handle": key},
        "metaobject": {"fields": [{"key": "data", "value": json.dumps(row, separators=(",", ":"))}]}})
    if result.get("metaobjectUpsert", {}).get("userErrors"):
        raise RuntimeError("Session write rejected")


def _merge(previous, body, identity, now):
    row = dict(previous)
    row.setdefault("started_at", now)
    row["last_seen_at"] = now
    # Exclusion is sticky, including anonymous activity before owner sign-in.
    row["excluded"] = bool(row.get("excluded") or identity["owner"] or body.get("exclude") is True)
    row["suspected_bot"] = bool(row.get("suspected_bot") or identity["bot"] or body.get("automated") is True)
    row["role_unknown"] = bool(identity["role_unknown"] or (row.get("role_unknown") and not identity["signed_in"]))
    row["signed_in"] = bool(row.get("signed_in") or identity["signed_in"])
    if identity["signed_in"] and not identity["role_unknown"]:
        row["store_handles"] = identity["stores"]
    row["timezone"] = re.sub(r"[^A-Za-z0-9_+\-/]", "", str(body.get("timezone") or ""))[:60]
    row["location"] = None  # A timezone/market is not a verified visit location.
    pages = {p["id"]: dict(p) for p in row.get("pages", [])}
    for item in body.get("pages", [])[-60:]:
        if not isinstance(item, dict) or not cc._SAFE_SESSION_ID.fullmatch(str(item.get("id") or "")):
            continue
        page_id = item["id"]
        page = pages.get(page_id, {"id": page_id, "at": now, "path": _path(item.get("path")), "events": []})
        handle = cc._normalized_handle(str(item.get("store_handle") or ""))
        if handle:
            page["store_handle"] = handle
        try:
            seconds = min(1800, max(0, int(item.get("active_seconds") or 0)))
        except (TypeError, ValueError, OverflowError):
            seconds = 0
        elapsed = max(0, int((cc._parse_datetime(now) - cc._parse_datetime(page["at"])).total_seconds()))
        page["active_seconds"] = max(page.get("active_seconds", 0), min(seconds, elapsed + 30))
        raw_events = item.get("events", [])
        if not isinstance(raw_events, list):
            raw_events = []
        page["events"] = sorted(set(page["events"]) | (set(str(e) for e in raw_events[:15]) & EVENTS))
        if item is body.get("pages", [])[-1]:
            page["last_at"] = now
            page["signed_in"] = identity["signed_in"] and not identity["role_unknown"] and not identity["owner"]
        pages[page_id] = page
    row["pages"] = list(pages.values())[-60:]
    row["pages_truncated"] = bool(row.get("pages_truncated") or len(pages) > 60)
    row["entry_page"] = row.get("entry_page") or (row["pages"][0]["path"] if row["pages"] else "/")
    row["exit_page"] = row["pages"][-1]["path"] if row["pages"] else "/"
    row["active_seconds"] = sum(p.get("active_seconds", 0) for p in row["pages"])
    row["duration_seconds"] = max(0, int((cc._parse_datetime(now) - cc._parse_datetime(row["started_at"])).total_seconds()))
    row["events"] = sorted(set(row.get("events", [])) | {e for p in row["pages"] for e in p["events"]})
    row["visited_stores"] = sorted(set(row.get("visited_stores", [])) | {p["store_handle"] for p in row["pages"] if p.get("store_handle")})
    return row


def recent(core, force=False):
    global _list_cache
    with _list_lock:
        if not force and _list_cache and time.monotonic() - _list_cache[0] < 60:
            return _list_cache[1]
        rows, cursor, truncated = [], None, False
        for _ in range(LIMIT // 100):
            result = core._shopify_graphql("""query($type:String!,$after:String){
              metaobjects(type:$type,first:100,after:$after,sortKey:"updated_at",reverse:true){
                nodes{handle fields{key value}} pageInfo{hasNextPage endCursor}}}""", {"type": TYPE, "after": cursor})
            conn = result.get("metaobjects") or {}
            for node in conn.get("nodes", []):
                try:
                    row = json.loads(cc._field_map(node).get("data") or "{}")
                    if row:
                        row["id"] = node["handle"]
                        rows.append(row)
                except (ValueError, TypeError):
                    continue
            info = conn.get("pageInfo") or {}
            truncated = bool(info.get("hasNextPage"))
            if not truncated or not info.get("endCursor"):
                break
            cursor = info["endCursor"]
        value = (rows, truncated)
        _list_cache = (time.monotonic(), value)
        return value


def visible(row):
    return not any(row.get(k) for k in ("excluded", "suspected_bot", "role_unknown"))


def summarize(rows, truncated, days=7, handle=""):
    now = datetime.now(timezone.utc)
    selected = [r for r in rows if visible(r) and cc._parse_datetime(r.get("last_seen_at"))
                and (now - cc._parse_datetime(r["last_seen_at"])).total_seconds() <= days * 86400
                and (not handle or handle in r.get("visited_stores", []))]
    selected.sort(key=lambda r: r["last_seen_at"], reverse=True)
    funnel = {e: sum(e in r.get("events", []) for r in selected) for e in ["create_opened", "create_started", "create_submitted", "create_accepted", "create_failed"]}
    exits = {}
    for row in selected:
        ended = (now - cc._parse_datetime(row["last_seen_at"])).total_seconds() >= 1800
        if ended and "create_opened" in row.get("events", []) and "create_accepted" not in row.get("events", []):
            exits[row["exit_page"]] = exits.get(row["exit_page"], 0) + 1
    summaries = [{k: v for k, v in r.items() if k != "pages"} | {
        "page_count": len(r.get("pages", [])),
        "status": "ended" if (now - cc._parse_datetime(r["last_seen_at"])).total_seconds() >= 1800 else "recent",
    } for r in selected]
    return {"ok": True, "sessions": summaries, "funnel": funnel, "exit_pages": sorted(exits.items(), key=lambda p: p[1], reverse=True),
            "sample_size": len(selected), "scanned": len(rows), "truncated": truncated,
            "excluded_count": sum(not visible(r) for r in rows), "days": days,
            "coverage": "Latest 1,000 recorded sessions; bot filtering is heuristic. Recent sessions may still be open. Exit pages show where tracking stopped, not why."}


def install_session_routes(app, core):
    @app.post("/api/activity/session")
    async def record(request: Request):
        denied = core._require_admin_secret(request)
        if denied is not None:
            return denied
        raw = await request.body()
        if len(raw) > 32000:
            return JSONResponse({"ok": False}, status_code=413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not cc._SAFE_SESSION_ID.fullmatch(str(body.get("session_id") or "")) or not isinstance(body.get("pages"), list):
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse({"ok": False}, status_code=400)
        identity = {"owner": request.headers.get("X-SS-Superadmin") == "1",
                    "role_unknown": request.headers.get("X-SS-Role-Unknown") == "1",
                    "signed_in": bool(request.headers.get("X-SS-Customer-Id")),
                    "stores": [h for h in request.headers.get("X-SS-Store-Handles", "").split(",") if cc._normalized_handle(h)],
                    "bot": bool(BOT.search(request.headers.get("X-SS-Visitor-UA", "")))}
        key = hashlib.sha256(body["session_id"].encode()).hexdigest()
        def save():
            with _locks[int(key[:4], 16) % len(_locks)]:
                _ensure(core)
                previous = _read(core, key)
                row = _merge(previous, body, identity, cc._utc_now())
                if not previous and (row["excluded"] or row["suspected_bot"]):
                    return {"ok": True, "excluded": True}
                _write(core, key, row)
            return {"ok": True}
        try:
            return await run_in_threadpool(save)
        except Exception:
            core.log.exception("Site session write unavailable")
            return JSONResponse({"ok": False}, status_code=502)

    @app.get("/admin/command-center/sessions")
    def sessions(request: Request, days: int = 7, handle: str = ""):
        denied = _denied(core, request)
        if denied is not None:
            return denied
        if days not in (1, 7, 30) or (handle and not cc._normalized_handle(handle)):
            return JSONResponse({"ok": False, "error": "Invalid filter"}, status_code=400)
        try:
            return summarize(*recent(core), days=days, handle=handle)
        except Exception:
            core.log.exception("Site session list unavailable")
            return JSONResponse({"ok": False, "error": "Sessions unavailable"}, status_code=502)

    @app.get("/admin/command-center/sessions/{key}")
    def session(key: str, request: Request):
        denied = _denied(core, request)
        if denied is not None:
            return denied
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            return JSONResponse({"ok": False}, status_code=400)
        try:
            row = _read(core, key)
            if not row or not visible(row):
                return JSONResponse({"ok": False, "error": "Session unavailable"}, status_code=404)
            return {"ok": True, "session": row}
        except Exception:
            return JSONResponse({"ok": False, "error": "Session unavailable"}, status_code=502)
