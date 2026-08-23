"""Authenticated, direct-to-Railway outreach store submission routes.

These routes reuse the normal storefront provisioning pipeline without
persisting prospect data or artwork in the deployment repository.  A caller
uploads the reviewed logo as multipart form data, receives a job id, and can
check the job through the authenticated status route.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from typing import Any, Dict
from urllib.parse import urlparse

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from outreach_assets import save_reviewed_logo_session
from outreach_auth import require_outreach_secret
import outreach_tracking


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INSTALL_LOCK = threading.Lock()
_INSTALL_ATTRIBUTE = "_stella_outreach_direct_routes_installed"

# Print files are 300 dpi, so -300 px raises the front artwork about one inch.
# Youth garments need more of it than adult ones: the body is shorter, so the
# same print zone puts the art nearer the middle of the chest than the top of
# it, and a logo floating at a child's stomach is the tell that nobody looked.
_PLACEMENT_DEFAULTS = {
    "bc3413_front_vertical_offset_px": -300,
    "cc1467y_front_vertical_offset_px": -500,
    "bc3001y_front_vertical_offset_px": -300,
}


def _placement_profile() -> Dict[str, int]:
    """The offsets, with an environment override per garment.

    Print placement is judged by looking at a mockup, not by reasoning about
    percentages, so this has to be adjustable without a deploy between each
    look.
    """
    profile = {}
    for key, fallback in _PLACEMENT_DEFAULTS.items():
        raw = os.getenv(key.upper(), "").strip()
        try:
            profile[key] = int(float(raw)) if raw else fallback
        except ValueError:
            profile[key] = fallback
    return profile


DEFAULT_PLACEMENT_PROFILE = _placement_profile()
def _required(value: Any, label: str, *, maximum: int = 300) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    if len(text) > maximum:
        raise ValueError(f"{label} is too long")
    return text


def _optional_url(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 2000:
        raise ValueError(f"{label} is too long")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an http(s) URL")
    return text


def _store_urls(handle: str) -> Dict[str, str]:
    return {
        "preview_url": f"https://stellasageco.com/collections/{handle}?preview=1",
        "claim_url": f"https://stellasageco.com/pages/join-store?shop={handle}",
    }


async def _save_reviewed_logo(core: Any, upload: UploadFile, handle: str) -> str:
    try:
        raw = await core._read_upload_limited(upload, core.MAX_UPLOAD_BYTES)
        if not raw:
            raise ValueError("logo upload was empty")
        image = core._pil_open_safe(raw).convert("RGBA")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("logo is not a valid image") from exc

    return save_reviewed_logo_session(core, image, handle)


def _update_tracking(core: Any, handle: str, patch: Dict[str, Any]) -> None:
    try:
        outreach_tracking.update(core, handle, patch)
    except Exception as exc:
        print(f"⚠️ direct outreach tracking update failed for {handle}: {exc}")


def _run_direct_job(
    core: Any,
    *,
    job_id: str,
    storefront_name: str,
    handle: str,
    type_of_store: str,
    primary_color: str,
    session_id: str,
) -> None:
    core._run_shopify_provision_job(
        job_id,
        storefront_name,
        handle,
        "",
        type_of_store,
        primary_color,
        session_id,
        None,
        dict(DEFAULT_PLACEMENT_PROFILE),
    )
    job = core._job_get(job_id) or {}
    if job.get("status") == "succeeded":
        _update_tracking(
            core,
            handle,
            {
                "built_at": outreach_tracking.utc_iso(),
                "status": "provisioned",
                "store_status": "prospect_unclaimed",
            },
        )
        return
    _update_tracking(
        core,
        handle,
        {
            "status": "failed",
            "build_error": str(job.get("error") or "Store provisioning failed")[:500],
        },
    )


def install_outreach_direct_routes(app: Any, core: Any) -> bool:
    """Install the authenticated machine-submission API once per FastAPI app."""
    with _INSTALL_LOCK:
        if getattr(app, _INSTALL_ATTRIBUTE, False):
            return False
        setattr(app, _INSTALL_ATTRIBUTE, True)

    @app.post("/api/outreach/storefront-request")
    async def direct_outreach_storefront_request(
        request: Request,
        contact_email: str = Form(...),
        storefront_name: str = Form(...),
        storefront_handle: str = Form(...),
        type_of_store: str = Form(...),
        primary_color: str = Form(...),
        organization_url: str = Form(""),
        contact_source_url: str = Form(""),
        logo_source_url: str = Form(""),
        screening_confirmed: bool = Form(...),
        logo_qa_confirmed: bool = Form(...),
        email_authorized: bool = Form(False),
        storefront_logo_file: UploadFile = File(...),
    ):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied

        try:
            handle = _required(storefront_handle, "storefront_handle", maximum=128).lower()
            if not _SAFE_HANDLE.fullmatch(handle):
                raise ValueError("storefront_handle contains unsupported characters")
            name = _required(storefront_name, "storefront_name")
            email = _required(contact_email, "contact_email")
            if not _EMAIL.fullmatch(email):
                raise ValueError("contact_email is invalid")
            store_type = _required(type_of_store, "type_of_store")
            color = _required(primary_color, "primary_color", maximum=80)
            if screening_confirmed is not True:
                raise ValueError("screening_confirmed=true is required")
            if logo_qa_confirmed is not True:
                raise ValueError("logo_qa_confirmed=true is required")
            source_fields = {
                "organization_url": _optional_url(
                    _required(organization_url, "organization_url", maximum=2000),
                    "organization_url",
                ),
                "contact_source_url": _optional_url(
                    _required(contact_source_url, "contact_source_url", maximum=2000),
                    "contact_source_url",
                ),
                "logo_source_url": _optional_url(
                    _required(logo_source_url, "logo_source_url", maximum=2000),
                    "logo_source_url",
                ),
            }
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        urls = _store_urls(handle)
        try:
            existing = core._get_custom_shop(handle)
        except Exception as exc:
            return JSONResponse(
                {"error": "Unable to check for an existing store", "detail": str(exc)[:300]},
                status_code=502,
            )
        if existing is not None:
            return {
                "status": "existing",
                "storefront_handle": handle,
                **urls,
            }

        try:
            state = outreach_tracking.read(core, handle)
        except Exception:
            state = {}
        if state.get("status") in {"queued", "building"} and state.get("job_id"):
            active_job = core._job_get(str(state["job_id"]))
            if active_job and active_job.get("status") in {"queued", "running"}:
                return {
                    "status": "already_queued",
                    "job_id": state["job_id"],
                    "storefront_handle": handle,
                    **urls,
                }

        try:
            session_id = await _save_reviewed_logo(core, storefront_logo_file, handle)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        job_id = str(uuid.uuid4())
        created_at = outreach_tracking.utc_iso()
        tracking_state = {
            "handle": handle,
            "job_id": job_id,
            "storefront_name": name,
            "contact_email": email,
            "source": "direct_outreach_api",
            "created_at": created_at,
            "built_at": None,
            "sent_at": None,
            "followup_due_at": None,
            "delete_due_at": None,
            "followup_sent_at": None,
            "status": "building",
            # Canonical server-controlled state for the anonymous prospect
            # admin demo.  The relay enables demo controls only while this
            # value and claim_status both remain unclaimed.
            "store_status": "prospect_unclaimed",
            "claim_status": "unclaimed",
            "placement_profile": dict(DEFAULT_PLACEMENT_PROFILE),
            "screening_confirmed": True,
            "logo_qa_confirmed": True,
            # False is the safe default. The initial email is sent separately
            # after store QA, then mark-sent enables the scheduled follow-up.
            "email_authorized": bool(email_authorized),
            **source_fields,
        }
        try:
            outreach_tracking.upsert(core, handle, tracking_state)
        except Exception as exc:
            return JSONResponse(
                {"error": "Unable to create outreach tracking", "detail": str(exc)[:300]},
                status_code=502,
            )

        core._job_set(
            job_id,
            status="queued",
            source="direct_outreach_api",
            storefront_name=name,
            storefront_handle=handle,
            contact_email=email,
            claimable=True,
            primary_color=color,
            main_session_id=session_id,
            created_at=time.time(),
        )
        threading.Thread(
            target=_run_direct_job,
            kwargs={
                "core": core,
                "job_id": job_id,
                "storefront_name": name,
                "handle": handle,
                "type_of_store": store_type,
                "primary_color": color,
                "session_id": session_id,
            },
            name=f"direct-outreach-{handle}",
            daemon=True,
        ).start()

        return {
            "status": "queued",
            "job_id": job_id,
            "storefront_handle": handle,
            "email_authorized": bool(email_authorized),
            **urls,
        }

    @app.get("/api/outreach/job/{job_id}")
    def direct_outreach_job_status(job_id: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        job = core._job_get(job_id)
        if not job:
            for handle, state in outreach_tracking.list_all(core).items():
                if str(state.get("job_id") or "") != job_id:
                    continue
                return {
                    "status": state.get("status") or "unknown",
                    "job_id": job_id,
                    "storefront_handle": handle,
                    "built_at": state.get("built_at"),
                    "build_error": state.get("build_error"),
                    "persisted": True,
                }
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return job

    @app.post("/api/outreach/store/{handle}/mark-sent")
    def mark_direct_outreach_sent(handle: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        normalized = str(handle or "").strip().lower()
        if not _SAFE_HANDLE.fullmatch(normalized):
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        state = outreach_tracking.read(core, normalized)
        if not state:
            return JSONResponse({"error": "Outreach tracking not found"}, status_code=404)
        sent_at = outreach_tracking.utc_iso()
        patch = {
            "sent_at": sent_at,
            "followup_due_at": outreach_tracking.add_days_iso(sent_at, 3),
            "delete_due_at": outreach_tracking.add_days_iso(sent_at, 7),
            "email_authorized": True,
            "status": "outreach_sent",
        }
        updated = outreach_tracking.update(core, normalized, patch)
        return {
            "status": "outreach_sent",
            "storefront_handle": normalized,
            "sent_at": updated.get("sent_at"),
            "followup_due_at": updated.get("followup_due_at"),
            "delete_due_at": updated.get("delete_due_at"),
        }

    return True
