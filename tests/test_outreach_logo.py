from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

import outreach_logo


class FakeCore:
    """Stands in for app.py. The real upscaler shells out to Real-ESRGAN."""

    MAX_IMAGE_PIXELS = 40_000_000

    def __init__(self, upscaler=True):
        self.upscale_calls = []
        if not upscaler:
            self._true_upscale_if_needed = None

    def _true_upscale_if_needed(self, image, target):
        self.upscale_calls.append((image.width, target))
        scale = target / image.width
        return image.resize(
            (target, max(1, round(image.height * scale))), Image.Resampling.LANCZOS
        )


def _logo(width, height=None, *, pad=0):
    """A mark on a transparent canvas, optionally with padding around it."""
    height = height or width
    canvas = Image.new("RGBA", (width + pad * 2, height + pad * 2), (0, 0, 0, 0))
    mark = Image.new("RGBA", (width, height), (200, 30, 40, 255))
    canvas.paste(mark, (pad, pad))
    return canvas


def _png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---- grading --------------------------------------------------------------


def test_a_big_logo_is_used_as_it_is():
    assert outreach_logo.assess(_logo(1400))["verdict"] == "original"


def test_a_small_but_sound_logo_is_enlarged():
    assert outreach_logo.assess(_logo(400))["verdict"] == "upscaled"


def test_a_tiny_logo_is_recreated():
    assert outreach_logo.assess(_logo(120))["verdict"] == "recreated"


def test_padding_is_not_mistaken_for_logo():
    """A 900px canvas holding a 150px mark is a 150px logo. Measuring the
    canvas is how a favicon on a big transparent square passes for artwork."""
    report = outreach_logo.assess(_logo(150, pad=400))

    assert report["artwork_width"] == 150
    assert report["verdict"] == "recreated"


def test_an_empty_image_is_not_a_logo():
    with pytest.raises(ValueError):
        outreach_logo.assess(Image.new("RGBA", (900, 900), (0, 0, 0, 0)))


# ---- the ladder -----------------------------------------------------------


def test_a_good_logo_reaches_the_print_standard():
    core = FakeCore()
    prepared, report = outreach_logo.prepare(core, _logo(1400))

    assert prepared.width == outreach_logo.TARGET_WIDTH
    assert report["verdict"] == "original"
    assert not report.get("recreated")


def test_a_small_logo_goes_through_the_real_upscaler():
    """The interactive uploader has always had Real-ESRGAN and this path never
    called it, so a 260px mark was stretched sixteen times by plain
    interpolation and shipped as a print file."""
    core = FakeCore()
    prepared, report = outreach_logo.prepare(core, _logo(300))

    assert core.upscale_calls, "the real upscaler was never asked"
    assert prepared.width == outreach_logo.TARGET_WIDTH
    assert report["verdict"] == "upscaled"


def test_the_output_is_cropped_to_the_artwork():
    """The garment builders want no padding, and the source usually has some."""
    core = FakeCore()
    prepared, _report = outreach_logo.prepare(core, _logo(1200, pad=300))

    bbox = prepared.getchannel("A").getbbox()
    assert bbox[0] <= 1 and bbox[1] <= 1
    assert bbox[2] >= prepared.width - 1


def test_a_missing_upscaler_still_produces_a_print_file():
    """Real-ESRGAN is a binary that may not be on every deployment. Losing it
    should cost sharpness, not the store."""
    core = FakeCore(upscaler=False)
    prepared, _report = outreach_logo.prepare(core, _logo(400))
    assert prepared.width == outreach_logo.TARGET_WIDTH


def test_fully_transparent_pixels_carry_no_color():
    core = FakeCore()
    prepared, _report = outreach_logo.prepare(core, _logo(1200, pad=100))

    import numpy as np
    rgba = np.asarray(prepared)
    invisible = rgba[:, :, 3] == 0
    if invisible.any():
        assert rgba[invisible][:, :3].max() == 0


# ---- recreation -----------------------------------------------------------


@pytest.fixture
def drawn(monkeypatch):
    """A stand-in image model that returns a clean transparent mark."""
    calls = []

    class Response:
        status_code = 200

        def json(self):
            image = _logo(900, pad=60)
            return {"data": [{"b64_json": base64.b64encode(_png_bytes(image)).decode()}]}

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(outreach_logo.requests, "post", post)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return calls


def test_a_tiny_logo_comes_back_at_print_quality(drawn):
    core = FakeCore()
    prepared, report = outreach_logo.prepare(core, _logo(90), organization="Oranco Bowmen")

    assert prepared.width == outreach_logo.TARGET_WIDTH
    assert report["verdict"] == "recreated"
    assert report["recreated"] is True


def test_their_own_artwork_is_what_gets_redrawn(drawn):
    """Asking a model to invent "a logo for an archery club" produces a logo
    for an archery club, which is precisely the wrong thing. The original goes
    in the request as the reference."""
    outreach_logo.prepare(FakeCore(), _logo(90), organization="Oranco Bowmen")

    assert drawn[0]["url"].endswith("/images/edits")
    assert "image" in drawn[0]["files"]
    prompt = drawn[0]["data"]["prompt"]
    assert "Oranco Bowmen" in prompt
    assert "transparent" in prompt.lower()
    assert drawn[0]["data"]["background"] == "transparent"


def test_a_recreated_logo_is_recorded_as_recreated(drawn):
    """The email has to disclose it, and by then the image is long gone."""
    _prepared, report = outreach_logo.prepare(FakeCore(), _logo(90))
    assert report["recreated"] is True


def test_an_opaque_drawing_is_refused(monkeypatch):
    """A logo on a white square prints as a white square."""
    class Response:
        status_code = 200

        def json(self):
            opaque = Image.new("RGBA", (900, 900), (255, 255, 255, 255))
            return {"data": [{"b64_json": base64.b64encode(_png_bytes(opaque)).decode()}]}

    monkeypatch.setattr(outreach_logo.requests, "post", lambda _u, **_k: Response())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with pytest.raises(ValueError, match="transparency"):
        outreach_logo.prepare(FakeCore(), _logo(90))


def test_a_failed_recreation_stops_the_build(monkeypatch):
    """Falling back to the blur would hide the problem behind a store that
    looks finished and prints badly."""
    class Response:
        status_code = 500
        text = "server error"

    monkeypatch.setattr(outreach_logo.requests, "post", lambda _u, **_k: Response())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with pytest.raises(ValueError, match="recreating it failed"):
        outreach_logo.prepare(FakeCore(), _logo(90))


def test_without_a_key_the_reason_says_so(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        outreach_logo.prepare(FakeCore(), _logo(90))


def test_vector_artwork_is_never_redrawn(drawn):
    """An SVG carries no resolution to lose. If it rasterized small that is a
    render setting, not damage, and redrawing their perfectly sharp mark would
    be a downgrade dressed up as a rescue."""
    core = FakeCore()
    prepared, report = outreach_logo.prepare(core, _logo(120), is_vector=True)

    assert drawn == []
    assert report["verdict"] == "upscaled"
    assert prepared.width == outreach_logo.TARGET_WIDTH


def test_a_good_logo_never_calls_the_image_model(drawn):
    """Recreation costs money per store. It is the last rung, not the first."""
    outreach_logo.prepare(FakeCore(), _logo(1400))
    assert drawn == []


# ---- the disclosure -------------------------------------------------------


def test_the_email_says_when_the_logo_was_redrawn(monkeypatch):
    import outreach_mail

    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    state = {"handle": "oranco", "storefront_name": "Oranco Bowmen Team Store",
             "contact_email": "info@oranco.org", "logo_recreated": True}
    body = outreach_mail.first_contact(state).get_content()

    assert "redrew" in body
    assert "original artwork" in body


def test_the_email_stays_quiet_when_the_logo_is_theirs(monkeypatch):
    """Saying "this is your real logo" in every other email would be strange,
    and it would make the disclosure look like boilerplate when it appears."""
    import outreach_mail

    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    state = {"handle": "oranco", "storefront_name": "Oranco Bowmen Team Store",
             "contact_email": "info@oranco.org"}
    body = outreach_mail.first_contact(state).get_content()

    assert "redrew" not in body
