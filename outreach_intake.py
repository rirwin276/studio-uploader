"""Durable, vendor-neutral outreach intake and Railway build worker.

OpenAI, Claude, or another approved research agent can submit the same small
JSON contract. The request is persisted to Shopify before work starts, while
logo bytes and prospect files never enter GitHub.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
import uuid
from typing import Any, Dict
from urllib.parse import urljoin, urlparse

import numpy as np
import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from PIL import Image

from outreach_assets import (
    MIN_REVIEWED_LOGO_WIDTH,
    save_reviewed_logo_session,
    validate_reviewed_logo,
)
from outreach_auth import require_outreach_secret
from outreach_direct import DEFAULT_PLACEMENT_PROFILE, _run_direct_job, _store_urls
import outreach_tracking


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_AGENT = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INSTALL_LOCK = threading.Lock()
_INSTALLED_APP_IDS: set[int] = set()
_WORKER_INSTALLED = False
_QUEUE_WAKE = threading.Event()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_HANDLES: set[str] = set()

MAX_REDIRECTS = 3
MIN_SOURCE_LOGO_WIDTH = 256
RECOVERY_AFTER_SECONDS = 30 * 60
QUEUE_STATES = {"intake_queued", "intake_processing"}


class OutreachIntakeError(ValueError):
    """A safe, caller-actionable intake or remote-asset error."""


def _required(payload: Dict[str, Any], key: str, *, maximum: int = 300) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise OutreachIntakeError(f"{key} is required")
    if len(value) > maximum:
        raise OutreachIntakeError(f"{key} is too long")
    return value


def _public_https_url(value: Any, label: str, *, resolve_dns: bool) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 2000:
        raise OutreachIntakeError(f"{label} is required and must be under 2000 characters")
    parsed = urlparse(text)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OutreachIntakeError(f"{label} must be a public HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise OutreachIntakeError(f"{label} must be a public HTTPS URL")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise OutreachIntakeError(f"{label} must be a public HTTPS URL")
    if not resolve_dns:
        return text
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, UnicodeError) as exc:
        raise OutreachIntakeError(f"{label} host could not be resolved") from exc
    if not addresses:
        raise OutreachIntakeError(f"{label} host could not be resolved")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as exc:
            raise OutreachIntakeError(f"{label} host returned an invalid address") from exc
        if not resolved.is_global:
            raise OutreachIntakeError(f"{label} must resolve only to public addresses")
    return text


def _normalize_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise OutreachIntakeError("request body must be a JSON object")
    allowed = {
        "provider_request_id",
        "source_agent",
        "contact_email",
        "storefront_name",
        "storefront_handle",
        "type_of_store",
        "primary_color",
        "organization_url",
        "contact_source_url",
        "logo_source_url",
        "screening_confirmed",
        "logo_source_reviewed",
        "email_authorized",
    }
    extras = sorted(str(key) for key in payload if key not in allowed)
    if extras:
        raise OutreachIntakeError(f"unsupported fields: {', '.join(extras)}")

    request_id = _required(payload, "provider_request_id", maximum=128)
    if not _SAFE_REQUEST_ID.fullmatch(request_id):
        raise OutreachIntakeError("provider_request_id contains unsupported characters")
    source_agent = _required(payload, "source_agent", maximum=40).lower()
    if not _SAFE_AGENT.fullmatch(source_agent):
        raise OutreachIntakeError("source_agent contains unsupported characters")
    handle = _required(payload, "storefront_handle", maximum=128).lower()
    if not _SAFE_HANDLE.fullmatch(handle):
        raise OutreachIntakeError("storefront_handle contains unsupported characters")
    email = _required(payload, "contact_email")
    if not _EMAIL.fullmatch(email):
        raise OutreachIntakeError("contact_email is invalid")
    if payload.get("screening_confirmed") is not True:
        raise OutreachIntakeError("screening_confirmed=true is required")
    if payload.get("logo_source_reviewed") is not True:
        raise OutreachIntakeError("logo_source_reviewed=true is required")
    if payload.get("email_authorized") not in {None, False}:
        raise OutreachIntakeError("email_authorized must remain false during intake")

    return {
        "provider_request_id": request_id,
        "source_agent": source_agent,
        "contact_email": email,
        "storefront_name": _required(payload, "storefront_name"),
        "storefront_handle": handle,
        "type_of_store": _required(payload, "type_of_store"),
        "primary_color": _required(payload, "primary_color", maximum=80),
        # Source URLs are recorded now and resolved only inside the worker. This
        # keeps intake fast and makes temporary DNS failures visible as job state.
        "organization_url": _public_https_url(
            payload.get("organization_url"),
            "organization_url",
            resolve_dns=False,
        ),
        "contact_source_url": _public_https_url(
            payload.get("contact_source_url"),
            "contact_source_url",
            resolve_dns=False,
        ),
        "logo_source_url": _public_https_url(
            payload.get("logo_source_url"),
            "logo_source_url",
            resolve_dns=False,
        ),
        "screening_confirmed": True,
        "logo_source_reviewed": True,
        "email_authorized": False,
    }


def _download_public_image(url: str, maximum_bytes: int) -> bytes:
    current = url
    session = requests.Session()
    # Do not allow deployment proxy variables to turn a public-logo fetch into
    # an implicit request to an internal network service.
    session.trust_env = False
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            _public_https_url(current, "logo_source_url", resolve_dns=True)
            with session.get(
                current,
                allow_redirects=False,
                stream=True,
                headers={"User-Agent": "Stella-Sage-Outreach-Intake/1.0"},
                timeout=(8, 30),
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_REDIRECTS:
                        raise OutreachIntakeError("logo_source_url redirected too many times")
                    location = str(response.headers.get("Location") or "").strip()
                    if not location:
                        raise OutreachIntakeError("logo_source_url returned an empty redirect")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise OutreachIntakeError(
                        f"logo_source_url returned HTTP {response.status_code}"
                    )
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                if content_type and not (
                    content_type.startswith("image/")
                    or content_type in {"application/octet-stream", "application/xml", "text/xml"}
                ):
                    raise OutreachIntakeError("logo_source_url did not return an image")
                content_length = str(response.headers.get("Content-Length") or "").strip()
                if content_length.isdigit() and int(content_length) > maximum_bytes:
                    raise OutreachIntakeError("logo_source_url exceeds the upload limit")
                body = bytearray()
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > maximum_bytes:
                        raise OutreachIntakeError("logo_source_url exceeds the upload limit")
                if not body:
                    raise OutreachIntakeError("logo_source_url returned an empty image")
                return bytes(body)
        raise OutreachIntakeError("logo_source_url could not be downloaded")
    except requests.RequestException as exc:
        raise OutreachIntakeError("logo_source_url download failed") from exc
    finally:
        session.close()


def _prepare_remote_logo(core: Any, raw: bytes) -> Image.Image:
    try:
        is_svg = bool(getattr(core, "_is_svg_data", lambda _raw: False)(raw))
        image = (
            core._svg_open_safe(raw)
            if is_svg
            else core._pil_open_safe(raw)
        ).convert("RGBA")
    except ValueError:
        raise OutreachIntakeError("logo_source_url did not contain a safe image")
    except Exception as exc:
        raise OutreachIntakeError("logo_source_url could not be decoded") from exc

    alpha = image.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if alpha_max == 0:
        raise OutreachIntakeError("logo image has no visible artwork")
    if alpha_min != 0:
        try:
            image = core._photoroom_remove_bg(image).convert("RGBA")
        except Exception as exc:
            raise OutreachIntakeError("automatic logo background removal failed") from exc

    alpha = image.getchannel("A")
    binary_alpha = alpha.point(lambda value: 255 if value > 6 else 0)
    bbox = binary_alpha.getbbox()
    if bbox is None:
        raise OutreachIntakeError("logo background removal left no visible artwork")
    image = image.crop(bbox)
    if image.width < MIN_SOURCE_LOGO_WIDTH and not is_svg:
        raise OutreachIntakeError(
            f"logo source must be at least {MIN_SOURCE_LOGO_WIDTH}px wide before upscaling"
        )

    target_width = MIN_REVIEWED_LOGO_WIDTH
    target_height = max(1, round(image.height * target_width / image.width))
    maximum_pixels = int(getattr(core, "MAX_IMAGE_PIXELS", 40_000_000))
    if target_width * target_height > maximum_pixels:
        raise OutreachIntakeError("prepared logo aspect ratio exceeds the image limit")
    if image.size != (target_width, target_height):
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)

    rgba = np.asarray(image.convert("RGBA")).copy()
    rgba[rgba[:, :, 3] == 0, :3] = 0
    prepared = Image.fromarray(rgba, "RGBA")
    try:
        return validate_reviewed_logo(
            prepared,
            max_pixels=maximum_pixels,
        )
    except ValueError as exc:
        raise OutreachIntakeError(str(exc)) from exc


def _track_failure(core: Any, handle: str, message: str) -> None:
    outreach_tracking.update(
        core,
        handle,
        {
            "status": "intake_failed",
            "failed_at": outreach_tracking.utc_iso(),
            "intake_error": str(message or "Intake processing failed")[:500],
        },
    )


def _process_intake_state(core: Any, handle: str, state: Dict[str, Any]) -> None:
    try:
        existing = core._get_custom_shop(handle)
    except Exception as exc:
        raise OutreachIntakeError("existing-store check failed") from exc
    if existing is not None:
        outreach_tracking.update(
            core,
            handle,
            {
                "status": "existing_store",
                "finished_at": outreach_tracking.utc_iso(),
            },
        )
        return

    attempts = int(state.get("intake_attempt_count") or 0) + 1
    outreach_tracking.update(
        core,
        handle,
        {
            "status": "intake_processing",
            "intake_started_at": outreach_tracking.utc_iso(),
            "intake_attempt_count": attempts,
            "intake_error": None,
        },
    )
    raw = _download_public_image(
        str(state.get("logo_source_url") or ""),
        int(getattr(core, "MAX_UPLOAD_BYTES", 12 * 1024 * 1024)),
    )
    prepared = _prepare_remote_logo(core, raw)
    session_id = save_reviewed_logo_session(core, prepared, handle)
    job_id = str(uuid.uuid4())
    outreach_tracking.update(
        core,
        handle,
        {
            "status": "building",
            "job_id": job_id,
            "main_session_id": session_id,
            "build_started_at": outreach_tracking.utc_iso(),
        },
    )
    core._job_set(
        job_id,
        status="queued",
        source="vendor_neutral_outreach_intake",
        source_agent=state.get("source_agent"),
        provider_request_id=state.get("provider_request_id"),
        storefront_name=state.get("storefront_name"),
        storefront_handle=handle,
        contact_email=state.get("contact_email"),
        claimable=True,
        primary_color=state.get("primary_color"),
        main_session_id=session_id,
        created_at=time.time(),
    )
    _run_direct_job(
        core,
        job_id=job_id,
        storefront_name=str(state.get("storefront_name") or ""),
        handle=handle,
        type_of_store=str(state.get("type_of_store") or ""),
        primary_color=str(state.get("primary_color") or ""),
        session_id=session_id,
    )


def _claim_handle(handle: str) -> bool:
    with _ACTIVE_LOCK:
        if handle in _ACTIVE_HANDLES:
            return False
        _ACTIVE_HANDLES.add(handle)
        return True


def _reconcile_stale_build(core: Any, handle: str, state: Dict[str, Any]) -> None:
    """Resolve interrupted builds without blindly repeating product creation."""
    job_id = str(state.get("job_id") or "")
    job = core._job_get(job_id) if job_id else {}
    if job and job.get("status") == "succeeded":
        outreach_tracking.update(
            core,
            handle,
            {
                "status": "provisioned",
                "built_at": state.get("built_at") or outreach_tracking.utc_iso(),
                "store_status": "prospect_unclaimed",
            },
        )
        return
    if job and job.get("status") in {"failed", "error"}:
        _track_failure(core, handle, str(job.get("error") or "Store build failed"))
        return
    try:
        existing = core._get_custom_shop(handle)
    except Exception:
        return
    if existing is not None:
        outreach_tracking.update(
            core,
            handle,
            {
                "status": "provisioned",
                "built_at": state.get("built_at") or outreach_tracking.utc_iso(),
                "store_status": "prospect_unclaimed",
            },
        )
        return
    _track_failure(
        core,
        handle,
        "Build was interrupted; inspect for partial products before retrying with a new request id",
    )


def process_intake_queue(core: Any, *, limit: int = 5) -> int:
    """Process queued or stale interrupted requests and return attempts started."""
    states = outreach_tracking.list_all(core)
    now = time.time()
    candidates: list[tuple[str, Dict[str, Any]]] = []
    for handle, state in states.items():
        status = str(state.get("status") or "")
        if status == "building":
            started = outreach_tracking.parse_iso(state.get("build_started_at"))
            if started is None or now - started.timestamp() >= RECOVERY_AFTER_SECONDS:
                try:
                    _reconcile_stale_build(core, handle, state)
                except Exception as exc:
                    print(f"⚠️ stale intake build reconciliation failed for {handle}: {exc}")
            continue
        if status == "intake_queued":
            candidates.append((handle, state))
            continue
        if status != "intake_processing":
            continue
        started = outreach_tracking.parse_iso(state.get("intake_started_at"))
        if started is None or now - started.timestamp() >= RECOVERY_AFTER_SECONDS:
            candidates.append((handle, state))
    candidates.sort(key=lambda item: str(item[1].get("created_at") or ""))

    attempted = 0
    for handle, state in candidates:
        if attempted >= max(1, limit) or not _claim_handle(handle):
            continue
        attempted += 1
        try:
            _process_intake_state(core, handle, state)
        except Exception as exc:
            try:
                _track_failure(core, handle, str(exc))
            except Exception as tracking_exc:
                print(f"⚠️ intake failure tracking unavailable for {handle}: {tracking_exc}")
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_HANDLES.discard(handle)
    return attempted


def install_outreach_intake_routes(app: Any, core: Any) -> bool:
    """Install the JSON intake and persistent-status endpoints once."""
    app_id = id(app)
    with _INSTALL_LOCK:
        if app_id in _INSTALLED_APP_IDS:
            return False
        _INSTALLED_APP_IDS.add(app_id)

    @app.post("/api/outreach/intake")
    async def create_outreach_intake(request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        try:
            payload = _normalize_payload(await request.json())
        except OutreachIntakeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"error": "request body must be valid JSON"}, status_code=400)

        handle = payload["storefront_handle"]
        urls = _store_urls(handle)
        try:
            existing_store = core._get_custom_shop(handle)
            existing_state = outreach_tracking.read(core, handle)
        except Exception:
            return JSONResponse(
                {"error": "Unable to check existing store and intake state"},
                status_code=502,
            )
        if existing_store is not None:
            return {"status": "existing", "storefront_handle": handle, **urls}
        if existing_state:
            same_request = (
                str(existing_state.get("provider_request_id") or "")
                == payload["provider_request_id"]
            )
            if same_request:
                return {
                    "status": existing_state.get("status") or "unknown",
                    "job_id": existing_state.get("job_id"),
                    "storefront_handle": handle,
                    "provider_request_id": payload["provider_request_id"],
                    **urls,
                }
            if existing_state.get("status") != "intake_failed":
                return JSONResponse(
                    {
                        "error": "storefront_handle already has a different active intake",
                        "storefront_handle": handle,
                    },
                    status_code=409,
                )

        created_at = outreach_tracking.utc_iso()
        state = {
            **payload,
            "handle": handle,
            "source": "vendor_neutral_outreach_intake",
            "created_at": created_at,
            "status": "intake_queued",
            "job_id": None,
            "built_at": None,
            "sent_at": None,
            "followup_due_at": None,
            "delete_due_at": None,
            "followup_sent_at": None,
            "store_status": "prospect_unclaimed",
            "claim_status": "unclaimed",
            "placement_profile": dict(DEFAULT_PLACEMENT_PROFILE),
            "intake_attempt_count": 0,
        }
        try:
            outreach_tracking.upsert(core, handle, state)
        except Exception:
            return JSONResponse(
                {"error": "Unable to persist outreach intake"},
                status_code=502,
            )
        _QUEUE_WAKE.set()
        return {
            "status": "intake_queued",
            "storefront_handle": handle,
            "provider_request_id": payload["provider_request_id"],
            "email_authorized": False,
            **urls,
        }

    @app.get("/api/outreach/intake/{handle}")
    def outreach_intake_status(handle: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        normalized = str(handle or "").strip().lower()
        if not _SAFE_HANDLE.fullmatch(normalized):
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        try:
            state = outreach_tracking.read(core, normalized)
        except Exception:
            return JSONResponse(
                {"error": "Unable to read outreach intake state"},
                status_code=502,
            )
        if not state:
            return JSONResponse({"error": "Outreach intake not found"}, status_code=404)
        return {
            "status": state.get("status") or "unknown",
            "job_id": state.get("job_id"),
            "storefront_handle": normalized,
            "provider_request_id": state.get("provider_request_id"),
            "source_agent": state.get("source_agent"),
            "created_at": state.get("created_at"),
            "built_at": state.get("built_at"),
            "intake_error": state.get("intake_error"),
            **_store_urls(normalized),
        }

    return True


def install_outreach_intake_worker(
    core: Any,
    *,
    delay_seconds: float = 3.0,
) -> bool:
    """Start the restart-recoverable queue worker once per process."""
    global _WORKER_INSTALLED
    with _INSTALL_LOCK:
        if _WORKER_INSTALLED:
            return False
        _WORKER_INSTALLED = True

    def worker() -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        interval = max(30, int(os.getenv("OUTREACH_INTAKE_INTERVAL_S", "60")))
        while True:
            try:
                process_intake_queue(core)
            except Exception as exc:
                print(f"⚠️ outreach intake scan failed: {exc}")
            _QUEUE_WAKE.wait(interval)
            _QUEUE_WAKE.clear()

    threading.Thread(
        target=worker,
        name="outreach-intake-worker",
        daemon=True,
    ).start()
    return True
