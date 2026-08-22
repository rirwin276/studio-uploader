"""Reviewed-logo validation and provisioning-session persistence."""

from __future__ import annotations

import time
import uuid
from typing import Any

from PIL import Image


MIN_REVIEWED_LOGO_WIDTH = 4096


def validate_reviewed_logo(
    image: Image.Image,
    *,
    max_pixels: int,
    minimum_width: int = MIN_REVIEWED_LOGO_WIDTH,
) -> Image.Image:
    """Return a normalized RGBA image or fail closed on incomplete logo QA."""
    reviewed = image.convert("RGBA")
    if reviewed.width < minimum_width:
        raise ValueError(f"reviewed logo must be at least {minimum_width}px wide")
    if reviewed.width * reviewed.height > max_pixels:
        raise ValueError("reviewed logo exceeds the image pixel limit")

    alpha = reviewed.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if alpha_min != 0 or alpha_max != 255:
        raise ValueError("reviewed logo must contain transparent and opaque pixels")
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError("reviewed logo has no visible artwork")
    tolerance_x = max(1, round(reviewed.width * 0.01))
    tolerance_y = max(1, round(reviewed.height * 0.01))
    if (
        bbox[0] > tolerance_x
        or bbox[1] > tolerance_y
        or reviewed.width - bbox[2] > tolerance_x
        or reviewed.height - bbox[3] > tolerance_y
    ):
        raise ValueError("reviewed logo still has excess transparent padding")
    return reviewed


def save_reviewed_logo_session(core: Any, image: Image.Image, handle: str) -> str:
    """Persist an already-reviewed 4K logo using the normal upload session shape."""
    reviewed = validate_reviewed_logo(
        image,
        max_pixels=int(getattr(core, "MAX_IMAGE_PIXELS", 40_000_000)),
    )
    session_id = f"outreach-{handle}-{uuid.uuid4().hex[:12]}"
    paths = core._paths(session_id)
    core._save_png(reviewed, paths["orig_master"])
    # Do not run the interactive editor's smaller normalization step. These
    # pixels already passed the strict outreach QA contract.
    core._save_png(reviewed, paths["orig_curr"])
    core._save_png(reviewed, paths["curr"])
    preview = reviewed.copy()
    preview.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
    core._save_png(preview, paths["orig_preview"])
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
