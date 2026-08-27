"""Public, reversible try-before-signup storefront demos.

This module deliberately sits beside the normal website request form and the
cold-outreach pipeline.  It reuses their proven Shopify provisioner and the
one-product prospect demo, but only records rows with the exact
``anonymous_demo`` source/state pair.  The public start route is inert unless
``ANONYMOUS_DEMO_ENABLED`` is explicitly enabled.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Iterable
from email.mime.text import MIMEText

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

import outreach_appearance
import outreach_tracking


SOURCE = outreach_tracking.ANONYMOUS_DEMO_SOURCE
STORE_STATUS = outreach_tracking.ANONYMOUS_DEMO_STORE_STATUS
_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INSTALL_LOCK = threading.Lock()
_INSTALL_ATTRIBUTE = "_stella_anonymous_demo_routes_installed"
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: Dict[str, Deque[float]] = defaultdict(deque)
_RATE_WINDOW_SECONDS = 60 * 60
_DEFAULT_RATE_LIMIT = 2
_TOKEN_VERSION = 1
_TOKEN_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_PRODUCT_TAG = "ss-anonymous-demo"


def enabled() -> bool:
    return os.getenv("ANONYMOUS_DEMO_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _secret() -> bytes:
    value = os.getenv("ANONYMOUS_DEMO_SECRET", "").strip()
    return value.encode("utf-8") if len(value) >= 32 else b""


def _allowed_origins() -> set[str]:
    raw = os.getenv(
        "ANONYMOUS_DEMO_ALLOWED_ORIGINS",
        "https://stellasageco.com,https://www.stellasageco.com",
    )
    return {item.strip().rstrip("/").lower() for item in raw.split(",") if item.strip()}


def _origin_allowed(request: Request) -> bool:
    origin = str(request.headers.get("origin") or "").strip().rstrip("/").lower()
    # Non-browser clients omit Origin.  The route still has rate/global caps;
    # tests and server-to-server health probes must remain possible.
    return not origin or origin in _allowed_origins()


def _client_key(request: Request) -> str:
    return str(getattr(getattr(request, "client", None), "host", "unknown") or "unknown")[:120]


def _rate_allowed(request: Request) -> bool:
    now = time.time()
    limit_raw = os.getenv("ANONYMOUS_DEMO_RATE_LIMIT_PER_HOUR", str(_DEFAULT_RATE_LIMIT))
    try:
        limit = max(1, min(20, int(limit_raw)))
    except ValueError:
        limit = _DEFAULT_RATE_LIMIT
    key = _client_key(request)
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[key]
        while bucket and bucket[0] <= now - _RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _make_token(handle: str) -> str:
    secret = _secret()
    if not secret:
        raise RuntimeError("Anonymous demo signing secret is not configured")
    now = int(time.time())
    payload = {
        "v": _TOKEN_VERSION,
        "h": handle,
        "n": secrets.token_urlsafe(18),
        "iat": now,
        "exp": now + _TOKEN_MAX_AGE_SECONDS,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify_token(token: str) -> Dict[str, Any]:
    secret = _secret()
    if not secret or not token or "." not in token:
        raise ValueError("invalid return link")
    encoded, supplied = token.split(".", 1)
    expected = _b64encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("invalid return link")
    try:
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid return link") from exc
    if payload.get("v") != _TOKEN_VERSION or int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("return link expired")
    handle = str(payload.get("h") or "").strip().lower()
    if not _SAFE_HANDLE.fullmatch(handle):
        raise ValueError("invalid return link")
    return payload


def _bearer(request: Request) -> str:
    value = str(request.headers.get("authorization") or "").strip()
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return (text or "team-store")[:72].rstrip("-")


def _store_urls(handle: str) -> Dict[str, str]:
    return {
        "preview_url": f"https://stellasageco.com/collections/{handle}?preview=1",
        "admin_url": f"https://stellasageco.com/pages/admin-powers?shop={handle}&prospect_demo=1",
        "claim_url": f"https://stellasageco.com/pages/join-store?shop={handle}",
    }


def _send_ready_email(email: str, name: str, token: str) -> None:
    """Send one transactional ready notice when the visitor opted in.

    The raw return token is never persisted server-side; it exists only in the
    browser and this short-lived worker argument. A failed email cannot make
    the build fail because the browser return link remains authoritative.
    """
    if not email:
        return
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "")
    if not host or not user or not password:
        print(f"[anonymous-demo] SMTP not configured; browser return link remains available for {name}")
        return
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    link = f"https://stellasageco.com/pages/start-team-store#resume={token}"
    message = MIMEText(
        f"Your temporary Stella & Sage store for {name} is ready.\n\n"
        f"Return to your demo: {link}\n\n"
        "You can customize it and build one product before signing in. "
        "Claim it within 48 hours of readiness to keep it.",
        "plain",
        "utf-8",
    )
    message["Subject"] = f"Your {name} demo store is ready"
    message["From"] = user
    message["To"] = email
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.sendmail(user, [email], message.as_string())
    except Exception as exc:
        print(f"[anonymous-demo] ready email failed for {name}: {exc}")


def _active_count(core: Any) -> int:
    maximum_raw = os.getenv("ANONYMOUS_DEMO_MAX_ACTIVE", "20")
    try:
        maximum = max(1, min(200, int(maximum_raw)))
    except ValueError:
        maximum = 20
    count = 0
    now = datetime.now(timezone.utc)
    for state in outreach_tracking.list_all(core).values():
        if not outreach_tracking.is_anonymous_demo_source(state.get("source")):
            continue
        if str(state.get("claim_status") or "unclaimed").lower() == "claimed":
            continue
        if str(state.get("status") or "").lower() in {"deleted", "expired", "failed"}:
            continue
        expiry = outreach_tracking.parse_iso(state.get("expires_at"))
        if expiry and expiry <= now:
            continue
        count += 1
    return count if count < maximum else maximum


def _at_capacity(core: Any) -> bool:
    maximum_raw = os.getenv("ANONYMOUS_DEMO_MAX_ACTIVE", "20")
    try:
        maximum = max(1, min(200, int(maximum_raw)))
    except ValueError:
        maximum = 20
    return _active_count(core) >= maximum


def _unique_handle(core: Any, name: str) -> str:
    base = _slug(name)
    for _attempt in range(8):
        handle = f"{base}-demo-{secrets.token_hex(3)}"[:120].rstrip("-")
        if core._get_custom_shop(handle) is not None:
            continue
        if outreach_tracking.read(core, handle):
            continue
        return handle
    raise RuntimeError("Unable to reserve a unique demo store name")


async def _save_logo_session(core: Any, upload: UploadFile, handle: str) -> str:
    raw = await core._read_upload_limited(upload, core.MAX_UPLOAD_BYTES)
    if not raw:
        raise ValueError("Logo upload was empty")
    image = core._pil_open_safe(raw).convert("RGBA")
    session_id = f"anonymous-{handle}-{uuid.uuid4().hex[:12]}"
    paths = core._paths(session_id)
    core._save_png(image, paths["orig_master"])
    core._build_original_assets(session_id)
    core._write_active_files(session_id, "original")
    core._sess_set(
        session_id,
        status="uploaded",
        stage="uploaded",
        created_at=time.time(),
        quality_flags=[],
        finalized=False,
        processing=False,
        processed_available=False,
        active_version="original",
        selected=True,
        error="",
    )
    return session_id


def _tag_products(core: Any, product_ids: Iterable[str]) -> None:
    mutation = """
    mutation TagAnonymousDemoProduct($id: ID!, $tags: [String!]!) {
      tagsAdd(id: $id, tags: $tags) { node { id } userErrors { field message } }
    }
    """
    for product_id in product_ids:
        if not product_id:
            continue
        result = core._shopify_graphql(mutation, {"id": product_id, "tags": [_PRODUCT_TAG]})
        errors = ((result.get("tagsAdd") or {}).get("userErrors")) or []
        if errors:
            raise RuntimeError("Unable to lock anonymous demo product checkout")


def tag_product_if_anonymous(core: Any, state: Dict[str, Any], product_id: str) -> None:
    if outreach_tracking.is_anonymous_demo_source(state.get("source")):
        _tag_products(core, [product_id])


def _tag_existing_store_products(core: Any, handle: str) -> None:
    query = """
    query AnonymousDemoProducts($query: String!, $after: String) {
      products(first: 100, query: $query, after: $after) {
        nodes { id }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    product_ids: list[str] = []
    cursor = None
    while True:
        result = core._shopify_graphql(query, {"query": f"tag:{handle}", "after": cursor})
        connection = result.get("products") or {}
        product_ids.extend(str(node.get("id") or "") for node in (connection.get("nodes") or []))
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            break
        cursor = str(page["endCursor"])
    _tag_products(core, product_ids)


def _run_build(
    core: Any,
    *,
    job_id: str,
    handle: str,
    name: str,
    store_type: str,
    color: str,
    session_id: str,
    contact_email: str = "",
    resume_token: str = "",
) -> None:
    core._run_shopify_provision_job(
        job_id,
        name,
        handle,
        "",
        store_type,
        color,
        session_id,
        None,
    )
    job = core._job_get(job_id) or {}
    if str(job.get("status") or "").lower() != "succeeded":
        outreach_tracking.update(core, handle, {
            "status": "failed",
            "build_error": str(job.get("error") or "Store provisioning failed")[:500],
            "failed_at": outreach_tracking.utc_iso(),
        })
        return

    try:
        outreach_appearance.apply(core, handle)
    except Exception as exc:
        print(f"[anonymous-demo] appearance update failed for {handle}: {exc}")

    # Checkout protection is not cosmetic. If the products cannot be tagged,
    # fail the demo closed instead of exposing an ownerless store for sale.
    try:
        _tag_existing_store_products(core, handle)
    except Exception as exc:
        print(f"[anonymous-demo] product checkout lock failed for {handle}: {exc}")
        outreach_tracking.update(core, handle, {
            "status": "failed",
            "build_error": "The demo store built, but checkout protection could not be applied.",
            "failed_at": outreach_tracking.utc_iso(),
        })
        return

    ready = datetime.now(timezone.utc)
    expires = ready + timedelta(hours=48)
    outreach_tracking.update(core, handle, {
        "status": "ready",
        "built_at": ready.isoformat(),
        "ready_at": ready.isoformat(),
        "expires_at": expires.isoformat(),
        "delete_due_at": expires.isoformat(),
        "store_status": STORE_STATUS,
    })
    if contact_email and resume_token:
        _send_ready_email(contact_email, name, resume_token)


def _public_status(handle: str, state: Dict[str, Any]) -> Dict[str, Any]:
    status = str(state.get("status") or "building").lower()
    phase = {
        "queued": "queued",
        "building": "building",
        "ready": "ready",
        "failed": "failed",
        "deleted": "expired",
        "expired": "expired",
        "claimed": "claimed",
    }.get(status, "building")
    if str(state.get("claim_status") or "unclaimed").lower() == "claimed":
        phase = "claimed"
    result: Dict[str, Any] = {
        "ok": True,
        "phase": phase,
        "storefront_name": state.get("storefront_name") or "Your team store",
        "storefront_handle": handle,
        "ready_at": state.get("ready_at"),
        "expires_at": state.get("expires_at"),
        "error": state.get("build_error") if phase == "failed" else None,
    }
    if phase in {"ready", "claimed"}:
        result.update(_store_urls(handle))
    return result


def install_anonymous_demo_routes(app: Any, core: Any) -> bool:
    with _INSTALL_LOCK:
        if getattr(app, _INSTALL_ATTRIBUTE, False):
            return False
        setattr(app, _INSTALL_ATTRIBUTE, True)

    @app.post("/api/demo/storefront-request")
    async def start_anonymous_demo(
        request: Request,
        storefront_name: str = Form(...),
        type_of_store: str = Form("team"),
        primary_color: str = Form("No preference"),
        email: str = Form(""),
        website: str = Form(""),
        storefront_logo_file: UploadFile = File(...),
    ):
        if not enabled():
            return JSONResponse({"error": "Try-before-signup is not available"}, status_code=404)
        if not _secret():
            return JSONResponse({"error": "Try-before-signup is not configured"}, status_code=503)
        if not _origin_allowed(request):
            return JSONResponse({"error": "This request must start on Stella & Sage"}, status_code=403)
        if website.strip():
            # Honeypot: answer generically so automated form fillers learn
            # nothing about why the request was discarded.
            return JSONResponse({"error": "Unable to start demo"}, status_code=400)
        name = storefront_name.strip()
        store_type = type_of_store.strip() or "team"
        color = primary_color.strip() or "No preference"
        contact_email = email.strip().lower()
        if not name or len(name) > 120:
            return JSONResponse({"error": "Store name is required and must be under 120 characters"}, status_code=400)
        if len(store_type) > 80 or len(color) > 80:
            return JSONResponse({"error": "Store type or color is too long"}, status_code=400)
        if contact_email and (len(contact_email) > 254 or not _EMAIL.fullmatch(contact_email)):
            return JSONResponse({"error": "Email address is invalid"}, status_code=400)
        if not _rate_allowed(request):
            return JSONResponse({"error": "Please wait before starting another demo"}, status_code=429)
        try:
            if _at_capacity(core):
                return JSONResponse(
                    {"error": "All demo build slots are busy. Please try again later."},
                    status_code=503,
                )
        except Exception:
            return JSONResponse({"error": "Unable to check demo availability"}, status_code=503)

        try:
            handle = _unique_handle(core, name)
            session_id = await _save_logo_session(core, storefront_logo_file, handle)
            token = _make_token(handle)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": "Unable to prepare demo", "detail": str(exc)[:200]}, status_code=502)

        job_id = str(uuid.uuid4())
        created_at = outreach_tracking.utc_iso()
        state = {
            "handle": handle,
            "job_id": job_id,
            "storefront_name": name,
            "contact_email": contact_email or None,
            "source": SOURCE,
            "created_at": created_at,
            "built_at": None,
            "ready_at": None,
            "expires_at": None,
            "delete_due_at": None,
            "status": "building",
            "store_status": STORE_STATUS,
            "claim_status": "unclaimed",
            "resume_token_hash": _token_hash(token),
            "prospect_demo": {
                "enabled": True,
                "product_limit": 1,
                "product_status": "available",
                "event_counts": {"anonymous_demo_started": 1},
                "events": [{"event": "anonymous_demo_started", "at": created_at}],
            },
        }
        try:
            outreach_tracking.upsert(core, handle, state)
        except Exception as exc:
            return JSONResponse({"error": "Unable to save demo", "detail": str(exc)[:200]}, status_code=502)

        core._job_set(
            job_id,
            status="queued",
            source=SOURCE,
            storefront_name=name,
            storefront_handle=handle,
            claimable=True,
            primary_color=color,
            main_session_id=session_id,
            created_at=time.time(),
        )
        threading.Thread(
            target=_run_build,
            kwargs={
                "core": core,
                "job_id": job_id,
                "handle": handle,
                "name": name,
                "store_type": store_type,
                "color": color,
                "session_id": session_id,
                "contact_email": contact_email,
                "resume_token": token,
            },
            name=f"anonymous-demo-{handle}",
            daemon=True,
        ).start()
        return {
            "status": "queued",
            "phase": "building",
            "job_id": job_id,
            "storefront_handle": handle,
            "resume_token": token,
            "resume_fragment": f"#resume={token}",
        }

    @app.get("/api/demo/status")
    def anonymous_demo_status(request: Request):
        token = _bearer(request)
        try:
            payload = _verify_token(token)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        handle = str(payload["h"])
        state = outreach_tracking.read(core, handle)
        if not state or not outreach_tracking.is_anonymous_demo_source(state.get("source")):
            return JSONResponse({"error": "Demo not found"}, status_code=404)
        if not hmac.compare_digest(str(state.get("resume_token_hash") or ""), _token_hash(token)):
            return JSONResponse({"error": "invalid return link"}, status_code=401)
        return _public_status(handle, state)

    return True
