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
import json
import re
import threading
import time
from collections import deque
from io import BytesIO
from pathlib import Path
from typing import Any, Dict

import numpy as np
from PIL import Image, UnidentifiedImageError


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SAFE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


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

    normalized = dict(payload)
    normalized.update(
        run_id=run_id,
        storefront_handle=handle,
        storefront_name=_required_text(payload, "storefront_name"),
        contact_email=_required_text(payload, "contact_email"),
        type_of_store=_required_text(payload, "type_of_store"),
        primary_color=_required_text(payload, "primary_color"),
        logo_path=logo_path,
        manifest_path=path,
        job_id=f"outreach-{run_id}",
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

    digest = hashlib.sha256(image.tobytes()).hexdigest()[:12]
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
    core._job_set(
        job_id,
        status="queued",
        source="repository_outreach_manifest",
        storefront_name=request["storefront_name"],
        storefront_handle=handle,
        contact_email=request["contact_email"],
        claimable=True,
        created_at=time.time(),
    )

    try:
        existing = core._get_custom_shop(handle)
    except Exception as exc:
        core._job_set(job_id, status="failed", finished_at=time.time(), error=f"Store lookup failed: {exc}")
        return

    if existing is not None:
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
    )


def process_pending_manifests(core: Any, outreach_root: Path) -> int:
    """Process enabled request files sequentially; return the number discovered."""
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
        _process_manifest(core, request)
    return len(paths)


def install_outreach_manifest_runner(core: Any, outreach_root: Path | None = None, delay_seconds: float = 3.0) -> bool:
    """Start one daemon worker when this deployment contains pending requests."""
    global _INSTALLED
    root = Path(outreach_root or (Path(__file__).resolve().parent / "outreach"))
    pending_dir = root / "pending"
    if not pending_dir.is_dir() or not any(pending_dir.glob("*.json")):
        return False

    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        _INSTALLED = True

    def worker() -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        process_pending_manifests(core, root)

    threading.Thread(target=worker, name="outreach-manifest-runner", daemon=True).start()
    return True
