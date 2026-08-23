from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import outreach_intake


class FakeCore:
    MAX_UPLOAD_BYTES = 12 * 1024 * 1024
    MAX_IMAGE_PIXELS = 40_000_000

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.existing = None
        self.jobs = {}
        self.sessions = {}
        self.provision_calls = []

    def _require_admin_secret(self, request: Request):
        if request.headers.get("X-Admin-Secret") != "test-secret":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    def _get_custom_shop(self, _handle):
        return self.existing

    def _paths(self, session_id):
        return {
            "orig_master": self.tmp_path / f"{session_id}_orig_master.png",
            "orig_preview": self.tmp_path / f"{session_id}_orig_preview.png",
            "orig_curr": self.tmp_path / f"{session_id}_orig_curr.png",
            "curr": self.tmp_path / f"{session_id}_curr.png",
        }

    def _save_png(self, image, path):
        image.save(path, format="PNG")

    def _sess_set(self, session_id, **state):
        self.sessions[session_id] = state

    def _job_set(self, job_id, **patch):
        self.jobs.setdefault(job_id, {}).update(patch)

    def _job_get(self, job_id):
        return self.jobs.get(job_id)

    def _run_shopify_provision_job(self, *args):
        self.provision_calls.append(args)
        self._job_set(args[0], status="succeeded")


def _reviewed_image() -> Image.Image:
    image = Image.new("RGBA", (4096, 1024), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((0, 0, 4095, 1023), fill=(20, 40, 80, 255))
    image.putpixel((0, 0), (0, 0, 0, 0))
    return image


def _payload(**overrides):
    payload = {
        "provider_request_id": "openai-20260822-example-club",
        "source_agent": "openai",
        "contact_email": "club@example.org",
        "storefront_name": "Example Club Team Store",
        "storefront_handle": "example-club",
        "type_of_store": "team handball",
        "primary_color": "Gray",
        "organization_url": "https://example.org/",
        "contact_source_url": "https://example.org/contact",
        "logo_source_url": "https://example.org/logo.png",
        "screening_confirmed": True,
        "logo_source_reviewed": True,
        "email_authorized": False,
    }
    payload.update(overrides)
    return payload


def _app_with_tracking(monkeypatch, tmp_path):
    states = {}

    def read(_core, handle):
        return dict(states.get(handle, {}))

    def upsert(_core, handle, state):
        states[handle] = dict(state)

    def update(_core, handle, patch):
        states.setdefault(handle, {}).update(patch)
        return dict(states[handle])

    def list_all(_core):
        return {handle: dict(state) for handle, state in states.items()}

    monkeypatch.setattr(outreach_intake.outreach_tracking, "read", read)
    monkeypatch.setattr(outreach_intake.outreach_tracking, "upsert", upsert)
    monkeypatch.setattr(outreach_intake.outreach_tracking, "update", update)
    monkeypatch.setattr(outreach_intake.outreach_tracking, "list_all", list_all)
    app = FastAPI()
    core = FakeCore(tmp_path)
    assert outreach_intake.install_outreach_intake_routes(app, core) is True
    return TestClient(app), core, states


def test_intake_requires_authentication(monkeypatch, tmp_path):
    client, core, _states = _app_with_tracking(monkeypatch, tmp_path)
    response = client.post("/api/outreach/intake", json=_payload())
    assert response.status_code == 401
    assert core.provision_calls == []


def test_intake_persists_before_worker_and_is_idempotent(monkeypatch, tmp_path):
    client, core, states = _app_with_tracking(monkeypatch, tmp_path)
    response = client.post(
        "/api/outreach/intake",
        headers={"X-Admin-Secret": "test-secret"},
        json=_payload(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "intake_queued"
    assert states["example-club"]["source_agent"] == "openai"
    assert states["example-club"]["email_authorized"] is False
    assert core.provision_calls == []

    duplicate = client.post(
        "/api/outreach/intake",
        headers={"X-Admin-Secret": "test-secret"},
        json=_payload(),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "intake_queued"


def test_worker_builds_through_normal_provisioner(monkeypatch, tmp_path):
    client, core, states = _app_with_tracking(monkeypatch, tmp_path)
    client.post(
        "/api/outreach/intake",
        headers={"X-Admin-Secret": "test-secret"},
        json=_payload(),
    )
    monkeypatch.setattr(outreach_intake, "_download_public_image", lambda *_args: b"logo")
    monkeypatch.setattr(
        outreach_intake, "_prepare_remote_logo",
        lambda *_args: (_reviewed_image(), {"verdict": "original"}),
    )

    assert outreach_intake.process_intake_queue(core) == 1

    assert len(core.provision_calls) == 1
    call = core.provision_calls[0]
    assert call[1:4] == ("Example Club Team Store", "example-club", "")
    assert call[-1] == outreach_intake.DEFAULT_PLACEMENT_PROFILE
    assert states["example-club"]["status"] == "provisioned"
    assert states["example-club"]["built_at"]


def test_intake_rejects_email_send_and_unknown_fields(monkeypatch, tmp_path):
    client, core, _states = _app_with_tracking(monkeypatch, tmp_path)
    response = client.post(
        "/api/outreach/intake",
        headers={"X-Admin-Secret": "test-secret"},
        json=_payload(email_authorized=True, unexpected="value"),
    )
    assert response.status_code == 400
    assert "unsupported fields" in response.json()["error"]
    assert core.provision_calls == []


def test_different_request_cannot_replace_active_handle(monkeypatch, tmp_path):
    client, core, _states = _app_with_tracking(monkeypatch, tmp_path)
    client.post(
        "/api/outreach/intake",
        headers={"X-Admin-Secret": "test-secret"},
        json=_payload(),
    )
    response = client.post(
        "/api/outreach/intake",
        headers={"X-Admin-Secret": "test-secret"},
        json=_payload(provider_request_id="claude-20260822-example-club", source_agent="claude"),
    )
    assert response.status_code == 409
    assert core.provision_calls == []


def test_logo_url_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(
        outreach_intake.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    try:
        outreach_intake._public_https_url(
            "https://assets.example.org/logo.png",
            "logo_source_url",
            resolve_dns=True,
        )
    except outreach_intake.OutreachIntakeError as exc:
        assert "public addresses" in str(exc)
    else:
        raise AssertionError("private DNS result should be rejected")


def test_background_removal_gets_a_transparent_edge_guard(monkeypatch):
    """A logo touching the edge of its source must not be cut by the removal
    provider before we have a chance to crop it correctly ourselves."""
    source = Image.new("RGB", (100, 40), (20, 50, 90))
    captured = []

    class LogoCore:
        MAX_IMAGE_PIXELS = 40_000_000

        @staticmethod
        def _is_svg_data(_raw):
            return False

        @staticmethod
        def _pil_open_safe(_raw):
            return source.copy()

        @staticmethod
        def _photoroom_remove_bg(image):
            captured.append(image.copy())
            return image

    monkeypatch.setattr(
        outreach_intake.outreach_logo,
        "prepare",
        lambda _core, image, **_kwargs: (image, {"verdict": "original"}),
    )
    monkeypatch.setattr(outreach_intake, "validate_reviewed_logo", lambda image, **_kwargs: image)

    prepared, _report = outreach_intake._prepare_remote_logo(LogoCore(), b"opaque-logo")

    assert captured
    assert captured[0].width > source.width
    assert captured[0].height > source.height
    assert prepared.size == source.size


def test_stale_processing_state_is_recovered(monkeypatch, tmp_path):
    _client, core, states = _app_with_tracking(monkeypatch, tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    states["example-club"] = {
        **_payload(),
        "handle": "example-club",
        "status": "intake_processing",
        "created_at": stale,
        "intake_started_at": stale,
    }
    monkeypatch.setattr(outreach_intake, "_download_public_image", lambda *_args: b"logo")
    monkeypatch.setattr(
        outreach_intake, "_prepare_remote_logo",
        lambda *_args: (_reviewed_image(), {"verdict": "original"}),
    )

    assert outreach_intake.process_intake_queue(core) == 1
    assert states["example-club"]["status"] == "provisioned"


def test_stale_build_is_not_blindly_repeated(monkeypatch, tmp_path):
    _client, core, states = _app_with_tracking(monkeypatch, tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    states["example-club"] = {
        **_payload(),
        "handle": "example-club",
        "status": "building",
        "created_at": stale,
        "build_started_at": stale,
        "job_id": "interrupted-job",
    }

    assert outreach_intake.process_intake_queue(core) == 0
    assert core.provision_calls == []
    assert states["example-club"]["status"] == "intake_failed"
    assert "partial products" in states["example-club"]["intake_error"]
