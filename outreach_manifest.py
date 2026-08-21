"""Repository-controlled, claimable outreach store runner.

An enabled JSON request under ``outreach/pending`` is treated as a deployment
job.  The runner is intentionally not exposed as a public HTTP endpoint: a
merge to the deployment repository is the authorization boundary.  Requests
are ownerless by construction and therefore enter the first-verified-claimant
flow implemented by the normal storefront join relay.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import smtplib
import threading
import time
from collections import deque
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from typing import Any, Dict

import numpy as np
from PIL import Image, UnidentifiedImageError

import outreach_tracking


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SAFE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
DEFAULT_PLACEMENT_PROFILE = {
    # Print files are 300 dpi; -300 moves the artwork up by approximately one
    # inch on the two garments used for cold-outreach previews.
    "bc3413_front_vertical_offset_px": -300,
    "cc1467y_front_vertical_offset_px": -300,
}


class OutreachManifestError(ValueError):
    """Raised when a repository outreach request is unsafe or incomplete."""


def _required_text(payload: Dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise OutreachManifestError(f"{key} is required")
    return value


def _load_manifest(path: Path, outreach_root: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutreachManifestError(f"invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise OutreachManifestError("manifest root must be an object")
    if payload.get("enabled") is not True:
        raise OutreachManifestError("manifest is not enabled")
    if payload.get("claimable") is not True:
        raise OutreachManifestError("outreach manifests must set claimable=true")

    run_id = _required_text(payload, "run_id").lower()
    handle = _required_text(payload, "storefront_handle").lower()
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise OutreachManifestError("run_id contains unsupported characters")
    if not _SAFE_HANDLE.fullmatch(handle):
        raise OutreachManifestError("storefront_handle contains unsupported characters")

    logo_rel = Path(_required_text(payload, "logo_base64_file"))
    if logo_rel.is_absolute():
        raise OutreachManifestError("logo_base64_file must be relative")
    logo_path = (outreach_root / logo_rel).resolve()
    root_resolved = outreach_root.resolve()
    if logo_path != root_resolved and root_resolved not in logo_path.parents:
        raise OutreachManifestError("logo_base64_file escapes the outreach directory")
    if not logo_path.is_file():
        raise OutreachManifestError(f"logo file not found: {logo_rel.as_posix()}")

    qa = payload.get("qa")
    if not isinstance(qa, dict) or qa.get("approved") is not True:
        raise OutreachManifestError("qa.approved=true is required")
    for flag in (
        "source_reviewed",
        "transparency_reviewed",
        "edge_quality_reviewed",
        "light_background_reviewed",
        "dark_background_reviewed",
        "garment_contrast_reviewed",
    ):
        if qa.get(flag) is not True:
            raise OutreachManifestError(f"qa.{flag}=true is required")
    approved_color = _required_text(qa, "approved_primary_color")
    primary_color = _required_text(payload, "primary_color")
    if approved_color.casefold() != primary_color.casefold():
        raise OutreachManifestError("qa.approved_primary_color must match primary_color")
    prepared_digest = _required_text(qa, "prepared_rgba_sha256").lower()
    if not _SHA256.fullmatch(prepared_digest):
        raise OutreachManifestError("qa.prepared_rgba_sha256 must be a lowercase SHA-256")
    minimum_width = int(qa.get("minimum_width") or 0)
    if minimum_width < 1 or minimum_width > 8192:
        raise OutreachManifestError("qa.minimum_width must be between 1 and 8192")

    placement_profile = payload.get("placement_profile") or dict(DEFAULT_PLACEMENT_PROFILE)
    if not isinstance(placement_profile, dict):
        raise OutreachManifestError("placement_profile must be an object")
    normalized_placement: Dict[str, float] = {}
    for key, default in DEFAULT_PLACEMENT_PROFILE.items():
        raw = placement_profile.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise OutreachManifestError(f"placement_profile.{key} must be numeric") from exc
        if value < -2000 or value > 2000:
            raise OutreachManifestError(f"placement_profile.{key} must be between -2000 and 2000")
        normalized_placement[key] = int(value) if value.is_integer() else value

    normalized = dict(payload)
    normalized.update(
        run_id=run_id,
        storefront_handle=handle,
        storefront_name=_required_text(payload, "storefront_name"),
        contact_email=_required_text(payload, "contact_email"),
        type_of_store=_required_text(payload, "type_of_store"),
        primary_color=primary_color,
        logo_path=logo_path,
        qa=dict(qa),
        manifest_path=path,
        job_id=f"outreach-{run_id}",
        placement_profile=normalized_placement,
    )
    return normalized


def _remove_connected_neutral_background(
    image: Image.Image,
    *,
    spread_max: int = 14,
    brightness_min: int = 32,
) -> Image.Image:
    """Remove only neutral pixels connected to the source image border.

    This avoids deleting enclosed white artwork such as letter counters or the
    white blades inside outlined oars. Transparent pixels are assigned black
    RGB values so later Lanczos resizing cannot reintroduce a white matte.
    """
    rgb = np.asarray(image.convert("RGB"))
    height, width, _channels = rgb.shape
    spread = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16)
    brightness = rgb.mean(axis=2)
    candidate = (spread <= spread_max) & (brightness >= brightness_min)

    background = np.zeros((height, width), dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        if candidate[0, x]:
            queue.append((0, x))
        if candidate[height - 1, x]:
            queue.append((height - 1, x))
    for y in range(height):
        if candidate[y, 0]:
            queue.append((y, 0))
        if candidate[y, width - 1]:
            queue.append((y, width - 1))

    while queue:
        y, x = queue.popleft()
        if background[y, x] or not candidate[y, x]:
            continue
        background[y, x] = True
        if y:
            queue.append((y - 1, x))
        if y + 1 < height:
            queue.append((y + 1, x))
        if x:
            queue.append((y, x - 1))
        if x + 1 < width:
            queue.append((y, x + 1))

    alpha = np.where(background, 0, 255).astype(np.uint8)
    if not np.any(alpha):
        raise OutreachManifestError("background cleanup removed the entire image")
    rgba = np.dstack((rgb, alpha))
    rgba[background, :3] = 0
    ys, xs = np.nonzero(alpha)
    cropped = rgba[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    return Image.fromarray(cropped, "RGBA")


def _prepare_logo(image: Image.Image, request: Dict[str, Any], max_pixels: int) -> Image.Image:
    settings = request.get("logo_preparation") or {}
    if not isinstance(settings, dict):
        raise OutreachManifestError("logo_preparation must be an object")

    prepared = image.convert("RGBA")
    if settings.get("remove_border_background") is True:
        prepared = _remove_connected_neutral_background(
            prepared,
            spread_max=int(settings.get("neutral_spread_max", 14)),
            brightness_min=int(settings.get("neutral_brightness_min", 32)),
        )

    target_width = int(settings.get("target_width") or prepared.width)
    if target_width < 1 or target_width > 8192:
        raise OutreachManifestError("logo target_width must be between 1 and 8192")
    if target_width != prepared.width:
        target_height = max(1, round(prepared.height * target_width / prepared.width))
        if target_width * target_height > max_pixels:
            raise OutreachManifestError("prepared logo dimensions exceed the image limit")
        prepared = prepared.resize((target_width, target_height), Image.Resampling.LANCZOS)
    # Canonicalize invisible RGB bytes. Different lossless containers may keep
    # arbitrary color values under alpha=0; zeroing them makes the reviewed
    # digest format-independent and prevents matte colors from leaking during
    # downstream resizing.
    rgba = np.asarray(prepared.convert("RGBA")).copy()
    rgba[rgba[:, :, 3] == 0, :3] = 0
    prepared = Image.fromarray(rgba, "RGBA")
    return prepared


def _write_session_logo(core: Any, request: Dict[str, Any]) -> str:
    logo_path: Path = request["logo_path"]
    try:
        encoded = logo_path.read_text(encoding="ascii")
        image_bytes = base64.b64decode("".join(encoded.split()), validate=True)
    except (OSError, UnicodeError, binascii.Error) as exc:
        raise OutreachManifestError(f"invalid base64 logo: {exc}") from exc

    if not image_bytes:
        raise OutreachManifestError("decoded logo is empty")
    max_bytes = int(getattr(core, "MAX_UPLOAD_BYTES", 12 * 1024 * 1024))
    if len(image_bytes) > max_bytes:
        raise OutreachManifestError("decoded logo exceeds the upload limit")

    try:
        with Image.open(BytesIO(image_bytes)) as opened:
            opened.load()
            width, height = opened.size
            max_pixels = int(getattr(core, "MAX_IMAGE_PIXELS", 40_000_000))
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise OutreachManifestError("logo dimensions exceed the image limit")
            image = _prepare_logo(opened, request, max_pixels)
    except (UnidentifiedImageError, OSError) as exc:
        raise OutreachManifestError("decoded logo is not a valid image") from exc

    qa = request["qa"]
    if image.width < int(qa["minimum_width"]):
        raise OutreachManifestError("prepared logo is narrower than qa.minimum_width")
    alpha = image.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if alpha_min != 0 or alpha_max != 255:
        raise OutreachManifestError("prepared logo must contain both transparent and opaque pixels")
    bbox = alpha.getbbox()
    if bbox is None:
        raise OutreachManifestError("prepared logo has no visible artwork")
    tolerance_x = max(1, round(image.width * 0.01))
    tolerance_y = max(1, round(image.height * 0.01))
    if (
        bbox[0] > tolerance_x
        or bbox[1] > tolerance_y
        or image.width - bbox[2] > tolerance_x
        or image.height - bbox[3] > tolerance_y
    ):
        raise OutreachManifestError("prepared logo still has excess transparent padding")
    prepared_digest = hashlib.sha256(image.tobytes()).hexdigest()
    if not hmac.compare_digest(prepared_digest, qa["prepared_rgba_sha256"]):
        raise OutreachManifestError("prepared logo does not match the QA-approved digest")

    digest = prepared_digest[:12]
    session_id = f"outreach-{request['run_id']}-{digest}"
    session_path = Path(core.UPLOAD_DIR) / f"{session_id}_curr.png"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = session_path.with_suffix(".tmp.png")
    image.save(temporary_path, format="PNG", optimize=True)
    temporary_path.replace(session_path)
    return session_id


def _process_manifest(core: Any, request: Dict[str, Any]) -> None:
    job_id = request["job_id"]
    handle = request["storefront_handle"]
    created_at = time.time()
    created_at_utc = str(request.get("sent_at_utc") or outreach_tracking.utc_iso(created_at))
    tracking_state = {
        "handle": handle,
        "storefront_name": request["storefront_name"],
        "contact_email": request["contact_email"],
        "source": "cold_outreach",
        "created_at": created_at_utc,
        "sent_at": created_at_utc,
        "followup_due_at": outreach_tracking.add_days_iso(created_at_utc, 3),
        "delete_due_at": outreach_tracking.add_days_iso(created_at_utc, 7),
        "followup_sent_at": None,
        "status": "building",
        "claim_status": "unclaimed",
        "placement_profile": request["placement_profile"],
        "email_authorized": bool(request.get("email_authorized", True)),
    }
    try:
        outreach_tracking.upsert(core, handle, tracking_state)
    except Exception as exc:
        print(f"⚠️ outreach tracking unavailable for {handle}: {exc}")
    core._job_set(
        job_id,
        status="queued",
        source="repository_outreach_manifest",
        storefront_name=request["storefront_name"],
        storefront_handle=handle,
        contact_email=request["contact_email"],
        claimable=True,
        created_at=created_at,
    )

    try:
        existing = core._get_custom_shop(handle)
    except Exception as exc:
        core._job_set(job_id, status="failed", finished_at=time.time(), error=f"Store lookup failed: {exc}")
        return

    if existing is not None:
        try:
            outreach_tracking.update(core, handle, {"status": "existing_store"})
        except Exception as exc:
            print(f"⚠️ outreach tracking update unavailable for {handle}: {exc}")
        core._job_set(
            job_id,
            status="skipped_existing",
            finished_at=time.time(),
            reason="A custom_shop with this handle already exists; no build was started.",
        )
        return

    try:
        session_id = _write_session_logo(core, request)
    except Exception as exc:
        core._job_set(job_id, status="failed", finished_at=time.time(), error=str(exc))
        return

    core._job_set(job_id, main_session_id=session_id)
    core._run_shopify_provision_job(
        job_id,
        request["storefront_name"],
        handle,
        "",
        request["type_of_store"],
        request["primary_color"],
        session_id,
        None,
        request["placement_profile"],
    )
    try:
        outreach_tracking.update(core, handle, {
            "built_at": outreach_tracking.utc_iso(),
            "status": "provisioned",
        })
    except Exception as exc:
        print(f"⚠️ outreach tracking completion unavailable for {handle}: {exc}")


def _followup_message(state: Dict[str, Any]) -> EmailMessage:
    handle = str(state.get("handle") or "").strip()
    name = str(state.get("storefront_name") or handle).strip()
    preview = f"https://stellasageco.com/collections/{handle}?preview=1"
    claim = f"https://stellasageco.com/pages/join-store?shop={handle}"
    message = EmailMessage()
    message["Subject"] = f"Quick follow-up: {name} private store"
    message["To"] = str(state.get("contact_email") or "").strip()
    message["From"] = os.getenv("SMTP_USER", "").strip()
    message.set_content(
        f"""Hello {name} team,

Just a quick follow-up on the private, unofficial {name} spirit-wear concept:
{preview}

If an authorized person claims the store, you can easily edit shirt and hoodie colors, move or resize the logo, add artwork, and add products from the admin dashboard. The first verified account to use the private claim link becomes the store administrator:
{claim}

If it is not useful, reply “no” and I will remove the private concept and artwork.

Ryan Irwin
Founder, Stella & Sage Co.
Veteran Owned and Operated
"""
    )
    return message


def process_due_followups(core: Any) -> int:
    """Send due follow-ups using SMTP; leave them pending when SMTP is unset."""
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not host or not user or not password:
        return 0
    now = time.time()
    sent = 0
    for handle, state in outreach_tracking.list_all(core).items():
        due = outreach_tracking.parse_iso(state.get("followup_due_at"))
        recipient = str(state.get("contact_email") or "").strip()
        if state.get("email_authorized", True) is not True or not due or due.timestamp() > now or state.get("followup_sent_at") or not recipient:
            continue
        message = _followup_message(state)
        try:
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(message)
            outreach_tracking.update(core, handle, {
                "followup_sent_at": outreach_tracking.utc_iso(),
                "status": "followup_sent",
            })
            sent += 1
        except Exception as exc:
            print(f"⚠️ follow-up failed for {handle}: {exc}")
    return sent


def process_pending_manifests(
    core: Any,
    outreach_root: Path,
    *,
    blocked_handles: set[str] | None = None,
) -> int:
    """Process enabled request files sequentially; return the number discovered."""
    blocked = blocked_handles or set()
    pending_dir = outreach_root / "pending"
    paths = sorted(pending_dir.glob("*.json")) if pending_dir.is_dir() else []
    for path in paths:
        fallback_job_id = f"outreach-invalid-{path.stem[:40]}"
        try:
            request = _load_manifest(path, outreach_root)
        except OutreachManifestError as exc:
            core._job_set(
                fallback_job_id,
                status="failed",
                source="repository_outreach_manifest",
                finished_at=time.time(),
                error=f"{path.name}: {exc}",
            )
            continue
        if request["storefront_handle"] in blocked:
            core._job_set(
                request["job_id"],
                status="skipped_retired",
                source="repository_outreach_manifest",
                storefront_handle=request["storefront_handle"],
                finished_at=time.time(),
                reason="The same deployment explicitly retires this handle; no rebuild was started.",
            )
            continue
        _process_manifest(core, request)
    return len(paths)


def _load_retire_manifest(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutreachManifestError(f"invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise OutreachManifestError("retire manifest root must be an object")
    if payload.get("enabled") is not True:
        raise OutreachManifestError("retire manifest is not enabled")
    if payload.get("action") != "delete_store":
        raise OutreachManifestError("retire manifest action must be delete_store")

    run_id = _required_text(payload, "run_id").lower()
    handle = _required_text(payload, "storefront_handle").lower()
    confirm_handle = _required_text(payload, "confirm_handle").lower()
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise OutreachManifestError("run_id contains unsupported characters")
    if not _SAFE_HANDLE.fullmatch(handle):
        raise OutreachManifestError("storefront_handle contains unsupported characters")
    if confirm_handle != handle:
        raise OutreachManifestError("confirm_handle must exactly match storefront_handle")

    normalized = dict(payload)
    normalized.update(
        run_id=run_id,
        storefront_handle=handle,
        job_id=f"outreach-retire-{run_id}",
        manifest_path=path,
    )
    return normalized


def process_retire_manifests(core: Any, outreach_root: Path) -> int:
    """Run explicitly confirmed store deletions before any pending builds."""
    retire_dir = outreach_root / "retire"
    paths = sorted(retire_dir.glob("*.json")) if retire_dir.is_dir() else []
    for path in paths:
        fallback_job_id = f"outreach-retire-invalid-{path.stem[:40]}"
        try:
            request = _load_retire_manifest(path)
        except OutreachManifestError as exc:
            core._job_set(
                fallback_job_id,
                status="failed",
                source="repository_outreach_retire_manifest",
                finished_at=time.time(),
                error=f"{path.name}: {exc}",
            )
            continue

        core._job_set(
            request["job_id"],
            status="queued",
            source="repository_outreach_retire_manifest",
            handle=request["storefront_handle"],
            reason=str(request.get("reason") or "").strip(),
            created_at=time.time(),
            log=[],
        )
        core._run_shopify_deprovision_job(request["job_id"], request["storefront_handle"])
    return len(paths)


def _retired_handles(outreach_root: Path) -> set[str]:
    """Return valid, explicitly confirmed handles retired by this deployment."""
    retire_dir = outreach_root / "retire"
    paths = sorted(retire_dir.glob("*.json")) if retire_dir.is_dir() else []
    handles: set[str] = set()
    for path in paths:
        try:
            handles.add(_load_retire_manifest(path)["storefront_handle"])
        except OutreachManifestError:
            continue
    return handles


def install_outreach_manifest_runner(core: Any, outreach_root: Path | None = None, delay_seconds: float = 3.0) -> bool:
    """Start the low-cost build/retire/follow-up worker for this deployment."""
    global _INSTALLED
    root = Path(outreach_root or (Path(__file__).resolve().parent / "outreach"))
    pending_dir = root / "pending"
    retire_dir = root / "retire"
    has_pending = pending_dir.is_dir() and any(pending_dir.glob("*.json"))
    has_retire = retire_dir.is_dir() and any(retire_dir.glob("*.json"))
    scheduler_enabled = os.getenv("OUTREACH_FOLLOWUP_SCHEDULER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "y"}
    if not has_pending and not has_retire and not scheduler_enabled:
        return False

    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        _INSTALLED = True

    def worker() -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        blocked_handles = _retired_handles(root)
        process_retire_manifests(core, root)
        process_pending_manifests(core, root, blocked_handles=blocked_handles)
        if not scheduler_enabled:
            return
        interval = max(300, int(os.getenv("OUTREACH_FOLLOWUP_INTERVAL_S", "900")))
        while True:
            try:
                process_due_followups(core)
            except Exception as exc:
                print(f"⚠️ outreach follow-up scan failed: {exc}")
            time.sleep(interval)

    threading.Thread(target=worker, name="outreach-manifest-runner", daemon=True).start()
    return True
