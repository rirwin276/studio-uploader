"""Getting a printable logo out of whatever was found online.

The standard does not move: a 4096px transparent PNG cropped to the artwork,
because that is what the garment builders need. What changes here is the
answer when the source cannot meet it.

The source is whatever a web search turned up — often a 180px navigation logo
saved as a JPEG in 2011. Three rungs, cheapest first:

  original   already big enough; clean it and use it
  upscaled   small but sound; put it through the real upscaler
  recreated  too little information left to enlarge; redraw it from the
             original as reference

The rung matters beyond this module. A recreated logo is our drawing of their
mark, not their file, and the outreach email says so — see outreach_mail. A
prospect who is told finds it honest; a prospect who notices by themselves
finds it something else.

Recreation needs OPENAI_API_KEY. Without it the ladder simply stops one rung
early and the store is not built, which is the behaviour this replaced.
"""

from __future__ import annotations

import base64
import io
import os
from typing import Any, Dict, Tuple

import requests
from PIL import Image


TARGET_WIDTH = 4096

# Wide enough that reaching 4096 is a modest enlargement rather than an
# invention. Below this the upscaler is guessing, but guessing from real
# structure, which is still their mark.
GOOD_ARTWORK_WIDTH = 900

# The floor. Under this there is not enough left to enlarge — the result is a
# smooth blur that reads as a bad print rather than a logo, and printing it on
# a garment is worse than not offering one.
SALVAGE_ARTWORK_WIDTH = 256

_IMAGE_API = "https://api.openai.com/v1/images/edits"
_IMAGE_MODEL = "gpt-image-1"
_IMAGE_SIZE = "1024x1024"
_IMAGE_TIMEOUT = (15, 180)

# Says redraw, not reinterpret. The failure this guards against is a model
# producing a nice logo that is not theirs.
_REDRAW_PROMPT = (
    "Redraw this exact logo as a clean, high-resolution, flat vector-style emblem. "
    "Reproduce the same shapes, symbols, lettering and colors as faithfully as "
    "possible. Use crisp edges and solid fills. The background must be fully "
    "transparent. Do not add any new elements, text, frames, borders, shadows, "
    "gradients, backgrounds or decoration. Do not crop or rearrange the design. "
    "Output only the logo artwork itself."
)


def recreation_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _crop_to_artwork(image: Image.Image) -> Image.Image:
    """Trim to the visible mark.

    Measuring before this would size the padding, not the logo: a 900px canvas
    holding a 200px mark is a 200px logo for every purpose that matters here.
    """
    alpha = image.convert("RGBA").getchannel("A")
    binary = alpha.point(lambda value: 255 if value > 6 else 0)
    bbox = binary.getbbox()
    if bbox is None:
        raise ValueError("logo has no visible artwork")
    return image.convert("RGBA").crop(bbox)


def assess(image: Image.Image) -> Dict[str, Any]:
    """How much real logo is in here, and what that allows."""
    cropped = _crop_to_artwork(image)
    width = cropped.width
    if width >= GOOD_ARTWORK_WIDTH:
        verdict = "original"
        reason = f"artwork is {width}px wide"
    elif width >= SALVAGE_ARTWORK_WIDTH:
        verdict = "upscaled"
        reason = f"artwork is only {width}px wide, so it was enlarged"
    else:
        verdict = "recreated"
        reason = (
            f"artwork is only {width}px wide, which is below the {SALVAGE_ARTWORK_WIDTH}px "
            "floor for enlarging"
        )
    return {"verdict": verdict, "reason": reason, "artwork_width": width,
            "artwork_height": cropped.height}


def _to_target(core: Any, image: Image.Image) -> Image.Image:
    """Reach 4096px wide, using the real upscaler when there is one.

    The interactive uploader has always had Real-ESRGAN and this path never
    called it, so a 260px mark was being stretched sixteen times by plain
    interpolation and shipped as a print file.
    """
    working = image.convert("RGBA")
    if working.width < TARGET_WIDTH:
        upscale = getattr(core, "_true_upscale_if_needed", None)
        if callable(upscale):
            try:
                working = upscale(working, TARGET_WIDTH).convert("RGBA")
            except Exception as exc:
                print(f"[outreach-logo] real upscale unavailable, using resize: {exc}")

    working = _crop_to_artwork(working)
    if working.width != TARGET_WIDTH:
        height = max(1, round(working.height * TARGET_WIDTH / working.width))
        working = working.resize((TARGET_WIDTH, height), Image.Resampling.LANCZOS)
    return working


def _flatten_invisible(image: Image.Image) -> Image.Image:
    """Zero the color under fully transparent pixels.

    Leftover color there prints as a halo the moment anything re-encodes the
    file without respecting alpha.
    """
    import numpy as np

    rgba = np.asarray(image.convert("RGBA")).copy()
    rgba[rgba[:, :, 3] == 0, :3] = 0
    return Image.fromarray(rgba, "RGBA")


def recreate(image: Image.Image, *, organization: str = "") -> Image.Image:
    """Redraw the mark from the original as reference.

    Sent as an edit rather than a text prompt so the model is working from
    their actual artwork. Asking a model to invent "a logo for a rowing club"
    produces a logo for a rowing club, which is precisely the wrong thing.
    """
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    buffer = io.BytesIO()
    # The model reads a modest reference perfectly well, and the source is
    # small by definition — this is the rung for logos that are too small.
    reference = image.convert("RGBA")
    reference.save(buffer, format="PNG")
    buffer.seek(0)

    prompt = _REDRAW_PROMPT
    if organization:
        prompt += f" The logo belongs to {organization}."

    response = requests.post(
        _IMAGE_API,
        headers={"Authorization": f"Bearer {key}"},
        files={"image": ("logo.png", buffer.getvalue(), "image/png")},
        data={
            "model": _IMAGE_MODEL,
            "prompt": prompt,
            "size": _IMAGE_SIZE,
            "background": "transparent",
            "n": "1",
        },
        timeout=_IMAGE_TIMEOUT,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"image model returned HTTP {response.status_code}: {response.text[:300]}"
        )

    payload = response.json()
    items = payload.get("data") or []
    if not items or not items[0].get("b64_json"):
        raise RuntimeError("image model returned no image")

    raw = base64.b64decode(items[0]["b64_json"])
    drawn = Image.open(io.BytesIO(raw)).convert("RGBA")

    alpha_min, alpha_max = drawn.getchannel("A").getextrema()
    if alpha_max == 0:
        raise RuntimeError("image model returned an empty image")
    if alpha_min == 255:
        # Opaque everywhere means it drew a background, and a logo on a white
        # square would print as a white square.
        raise RuntimeError("image model returned an image with no transparency")
    return drawn


def prepare(
    core: Any,
    image: Image.Image,
    *,
    organization: str = "",
    is_vector: bool = False,
) -> Tuple[Image.Image, Dict[str, Any]]:
    """Take a cleaned logo up to the print standard, however that has to happen.

    Returns the 4096px image and a report naming the rung used, which the
    ledger stores and the email reads.
    """
    report = assess(image)
    cropped = _crop_to_artwork(image)

    if is_vector and report["verdict"] == "recreated":
        # Vector artwork carries no resolution to lose, so there is nothing to
        # reconstruct — it only ever rasterized small. Redrawing their real,
        # perfectly sharp mark would be a downgrade dressed up as a rescue.
        report["verdict"] = "upscaled"
        report["reason"] = "vector artwork, rasterized and scaled without loss"

    if report["verdict"] in {"original", "upscaled"}:
        return _flatten_invisible(_to_target(core, cropped)), report

    if not recreation_available():
        raise ValueError(
            f"{report['reason']}, and recreation is unavailable "
            "(OPENAI_API_KEY is not configured)"
        )

    try:
        drawn = recreate(cropped, organization=organization)
    except Exception as exc:
        # Falling back to the blur would hide the problem behind a store that
        # looks finished and prints badly.
        raise ValueError(f"{report['reason']}, and recreating it failed: {exc}") from exc

    report["recreated"] = True
    return _flatten_invisible(_to_target(core, drawn)), report
