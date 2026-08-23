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
import colorsys
import io
import math
import os
from typing import Any, Dict, List, Tuple

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
# Environment-driven for the same reason the discovery model is: names change
# on the provider's schedule, not ours, and a better one should be a variable
# edit rather than a deploy.
DEFAULT_IMAGE_MODEL = "gpt-image-1"
_IMAGE_SIZE = "1024x1024"
_IMAGE_TIMEOUT = (15, 240)

# The lever that matters for a logo. Faithfulness, not creativity, is the whole
# job here — a beautiful redraw of the wrong mark is a failure. Sent only when
# the endpoint accepts it; see recreate().
_FIDELITY = "high"

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


def _domain(url: Any) -> str:
    """The lowercased host of a URL, without www."""
    text = str(url or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    host = text.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _registrable(host: str) -> str:
    """The part of a host that identifies who owns it.

    Deliberately a heuristic. assets.westside-rowing.org and westside-rowing.org
    are the same people; westside-rowing.org and koelner-scheibengolf.de are not,
    and that is the only distinction being made here.
    """
    labels = [part for part in str(host or "").split(".") if part]
    if len(labels) < 2:
        return host or ""
    # co.uk, org.au, org.uk and friends need one more label to say anything.
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"} and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# Hosting a logo on a site builder's CDN is normal for a small club, and the
# CDN host says nothing about who owns the image. These are allowed through and
# marked, rather than trusted or rejected outright.
_BUILDER_CDNS = (
    "squarespace.com", "squarespace-cdn.com", "wixstatic.com", "wix.com",
    "shopify.com", "googleusercontent.com", "cloudfront.net", "amazonaws.com",
    "weebly.com", "wordpress.com", "wp.com", "sportsengine.com", "teamsnap.com",
    "leagueapps.com", "sitewrench.com", "godaddysites.com", "img1.wsimg.com",
    "cloudinary.com", "imgix.net", "netlify.app", "github.io",
)


def _logo_origin(logo_url: str, organization_url: str) -> Tuple[str, str]:
    """Whether this logo plausibly belongs to this organization.

    The brief already tells the model to take the logo from the organization's
    own site. That is not the same as it being true, and the failure is silent
    and expensive: a store gets built and emailed wearing a different club's
    badge, which reads as a scrape rather than as effort. A logo from an
    unrelated domain is almost always the model having found a similarly-named
    organization.
    """
    logo_host = _domain(logo_url)
    org_host = _domain(organization_url)
    if not logo_host or not org_host:
        return "unknown", "logo or organization URL has no host"

    logo_site = _registrable(logo_host)
    if logo_site == _registrable(org_host):
        return "own_domain", ""
    if any(logo_site == cdn or logo_host.endswith("." + cdn) for cdn in _BUILDER_CDNS):
        return "cdn", ""
    return "foreign", f"logo is hosted on {logo_host}, which is not {org_host} or a site-builder CDN"


def logo_origin(logo_url: str, organization_url: str) -> Tuple[str, str]:
    """Public name for the origin check. See _logo_origin."""
    return _logo_origin(logo_url, organization_url)


# A mark is flat: a handful of colours cover almost all of it, however busy the
# drawing is. A photograph is continuous tone and no small set of colours covers
# much of anything. Measured on a detailed crest with a gradient ring and
# antialiasing this sits near 0.97, and on a photograph near 0.21, so the line
# between them is not a fine judgement.
FLATNESS_FLOOR = 0.50
_FLATNESS_TOP_COLORS = 8
_FLATNESS_BUCKETS = 6
_FLATNESS_SAMPLES = 120


def flatness(image: Image.Image) -> Tuple[float, int]:
    """What share of the visible artwork the few most common colours cover."""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    step = 256 // _FLATNESS_BUCKETS
    counts: Dict[Tuple[int, int, int], int] = {}
    total = 0
    for y in range(0, height, max(1, height // _FLATNESS_SAMPLES)):
        for x in range(0, width, max(1, width // _FLATNESS_SAMPLES)):
            red, green, blue, alpha = pixels[x, y]
            if alpha < 128:
                continue
            key = (red // step, green // step, blue // step)
            counts[key] = counts.get(key, 0) + 1
            total += 1
    if not total:
        return 0.0, 0
    top = sorted(counts.values(), reverse=True)[:_FLATNESS_TOP_COLORS]
    return sum(top) / total, len(counts)


def looks_photographic(image: Image.Image) -> Tuple[bool, str]:
    """Whether this is a photograph rather than a logo.

    One store was built from a half-erased photo of a man swimming, lifted from
    a hero banner. It passed every other check — it was on the right domain and
    it was plenty big — and printing it on eight garments produced something
    worse than no store at all. Nothing until now asked whether the image was
    a mark.
    """
    covered, distinct = flatness(image)
    if covered < FLATNESS_FLOOR:
        return True, (
            f"the image looks like a photograph rather than a logo — its "
            f"{_FLATNESS_TOP_COLORS} commonest colours cover only "
            f"{covered:.0%} of it, across {distinct} distinct tones"
        )
    return False, ""


# ─── Brand colours ──────────────────────────────────────────────────────────
# The organization's colours are sitting in their logo. Reading them there is
# free and exact, where the intake's primary_color is one word a model guessed
# — "Red" for a department whose actual red is #b6001e.

_BRAND_BUCKETS = 12
_BRAND_SAMPLES = 160
# Two colours closer than this read as one colour to a person, so returning
# both would give a store two shades of the same thing and call it variety.
_BRAND_MIN_DISTANCE = 26.0


def _srgb_to_lab(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    def linear(channel: float) -> float:
        channel /= 255.0
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(float(c)) for c in rgb)
    x = (red * 0.4124 + green * 0.3576 + blue * 0.1805) / 0.95047
    y = red * 0.2126 + green * 0.7152 + blue * 0.0722
    z = (red * 0.0193 + green * 0.1192 + blue * 0.9505) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + (16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _lab_distance(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
    la, aa, ba = _srgb_to_lab(a)
    lb, ab, bb = _srgb_to_lab(b)
    return math.sqrt((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2)


def _hex(rgb: Tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def brand_colors(image: Image.Image, count: int = 3) -> List[str]:
    """The colours this logo is actually made of, most prominent first.

    Ranked by coverage weighted toward saturation, so a crest that is mostly
    off-white paper with a red cross returns the red. A genuinely monochrome
    mark still returns its greys rather than nothing, because a black-and-white
    logo's brand colour really is black.
    """
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    step = 256 // _BRAND_BUCKETS
    tally: Dict[Tuple[int, int, int], List[float]] = {}

    for y in range(0, height, max(1, height // _BRAND_SAMPLES)):
        for x in range(0, width, max(1, width // _BRAND_SAMPLES)):
            red, green, blue, alpha = pixels[x, y]
            if alpha < 200:
                continue
            key = (red // step, green // step, blue // step)
            entry = tally.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            entry[0] += red
            entry[1] += green
            entry[2] += blue
            entry[3] += 1

    if not tally:
        return []

    candidates = []
    for totals in tally.values():
        pixel_count = totals[3]
        average = (
            int(totals[0] / pixel_count),
            int(totals[1] / pixel_count),
            int(totals[2] / pixel_count),
        )
        _hue, saturation, value = colorsys.rgb_to_hsv(*(c / 255 for c in average))
        # A colour earns its place by how much of the mark it covers and how
        # much of a colour it is. Pure coverage alone returns the white paper
        # every crest is printed on.
        weight = pixel_count * (0.35 + saturation)
        # Near-black and near-white are structure — outlines and paper — before
        # they are brand, so they only win when there is nothing else.
        if value < 0.12 or (saturation < 0.12 and value > 0.9):
            weight *= 0.25
        candidates.append((weight, average))

    candidates.sort(key=lambda item: item[0], reverse=True)

    chosen: List[Tuple[int, int, int]] = []
    for _weight, rgb in candidates:
        if all(_lab_distance(rgb, taken) >= _BRAND_MIN_DISTANCE for taken in chosen):
            chosen.append(rgb)
        if len(chosen) >= count:
            break
    return [_hex(rgb) for rgb in chosen]


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

    model = os.getenv("OUTREACH_LOGO_MODEL", DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL
    fields = {
        "model": model,
        "prompt": prompt,
        "size": _IMAGE_SIZE,
        "background": "transparent",
        "n": "1",
        "input_fidelity": _FIDELITY,
    }

    def send(data):
        return requests.post(
            _IMAGE_API,
            headers={"Authorization": f"Bearer {key}"},
            files={"image": ("logo.png", buffer.getvalue(), "image/png")},
            data=data,
            timeout=_IMAGE_TIMEOUT,
        )

    response = send(fields)
    if response.status_code >= 300 and "input_fidelity" in (response.text or ""):
        # Not every model or API version takes this parameter. Losing fidelity
        # is worth a redraw; losing the redraw over one rejected field is not.
        print("[outreach-logo] input_fidelity not accepted, retrying without it")
        fields.pop("input_fidelity", None)
        response = send(fields)

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
    cropped = _crop_to_artwork(image)

    # Asked before anything else. Enlarging a photograph gives a bigger
    # photograph, and redrawing one gives a drawing of a photograph; neither is
    # a logo, and both cost money to find that out.
    photographic, photo_problem = looks_photographic(cropped)
    if photographic:
        raise ValueError(photo_problem)

    report = assess(image)
    # Read off the source, before any upscaling or redrawing can shift them.
    report["brand_colors"] = brand_colors(cropped)

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
