from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import outreach_direct


class ImmediateThread:
    def __init__(self, *, target, kwargs, **_unused):
        self.target = target
        self.kwargs = kwargs

    def start(self):
        self.target(**self.kwargs)


class FakeCore:
    MAX_UPLOAD_BYTES = 12 * 1024 * 1024
    MAX_IMAGE_PIXELS = 40_000_000

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.jobs = {}
        self.sessions = {}
        self.existing = None
        self.provision_calls = []

    def _require_admin_secret(self, request: Request):
        if request.headers.get("X-Admin-Secret") != "test-secret":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    async def _read_upload_limited(self, upload, maximum):
        raw = await upload.read()
        if len(raw) > maximum:
            raise ValueError("too_large")
        return raw

    def _pil_open_safe(self, raw):
        image = Image.open(BytesIO(raw))
        image.load()
        return image

    def _paths(self, session_id):
        return {
            "orig_master": self.tmp_path / f"{session_id}_orig_master.png",
            "orig_preview": self.tmp_path / f"{session_id}_orig_preview.png",
            "orig_curr": self.tmp_path / f"{session_id}_orig_curr.png",
            "curr": self.tmp_path / f"{session_id}_curr.png",
        }

    def _save_png(self, image, path):
        image.save(path, format="PNG")

    def _build_original_assets(self, _session_id):
        return None

    def _write_active_files(self, _session_id, _version):
        return None

    def _sess_set(self, session_id, **state):
        self.sessions[session_id] = state

    def _get_custom_shop(self, _handle):
        return self.existing

    def _job_set(self, job_id, **patch):
        self.jobs.setdefault(job_id, {}).update(patch)

    def _job_get(self, job_id):
        return self.jobs.get(job_id)

    def _run_shopify_provision_job(self, *args):
        self.provision_calls.append(args)
        self._job_set(args[0], status="succeeded")


def _logo_bytes(*, padded: bool = False) -> bytes:
    image = Image.new("RGBA", (4096, 1024), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    bounds = (400, 100, 3696, 924) if padded else (0, 0, 4095, 1023)
    draw.ellipse(bounds, fill=(20, 40, 80, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


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

    monkeypatch.setattr(outreach_direct.outreach_tracking, "read", read)
    monkeypatch.setattr(outreach_direct.outreach_tracking, "upsert", upsert)
    monkeypatch.setattr(outreach_direct.outreach_tracking, "update", update)
    monkeypatch.setattr(outreach_direct.outreach_tracking, "list_all", list_all)
    monkeypatch.setattr(outreach_direct.threading, "Thread", ImmediateThread)

    app = FastAPI()
    core = FakeCore(tmp_path)
    assert outreach_direct.install_outreach_direct_routes(app, core) is True
    return TestClient(app), core, states


def _valid_form():
    return {
        "contact_email": "club@example.org",
        "storefront_name": "Example Club Team Store",
        "storefront_handle": "example-club",
        "type_of_store": "team handball",
        "primary_color": "Gray",
        "organization_url": "https://example.org/",
        "contact_source_url": "https://example.org/contact",
        "logo_source_url": "https://example.org/logo.png",
        "screening_confirmed": "true",
        "logo_qa_confirmed": "true",
    }


def test_direct_request_requires_admin_secret(monkeypatch, tmp_path):
    client, core, _states = _app_with_tracking(monkeypatch, tmp_path)
    response = client.post(
        "/api/outreach/storefront-request",
        data=_valid_form(),
        files={"storefront_logo_file": ("logo.png", _logo_bytes(), "image/png")},
    )
    assert response.status_code == 401
    assert core.provision_calls == []


def test_direct_request_builds_claimable_store_and_tracks_it(monkeypatch, tmp_path):
    client, core, states = _app_with_tracking(monkeypatch, tmp_path)
    response = client.post(
        "/api/outreach/storefront-request",
        headers={"X-Admin-Secret": "test-secret"},
        data=_valid_form(),
        files={"storefront_logo_file": ("logo.png", _logo_bytes(), "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["email_authorized"] is False
    assert payload["preview_url"].endswith("/collections/example-club?preview=1")
    assert payload["claim_url"].endswith("/pages/join-store?shop=example-club")

    assert len(core.provision_calls) == 1
    call = core.provision_calls[0]
    assert call[1:4] == ("Example Club Team Store", "example-club", "")
    assert call[-1] == outreach_direct.DEFAULT_PLACEMENT_PROFILE
    with Image.open(core._paths(call[6])["curr"]) as uploaded:
        assert uploaded.size == (4096, 1024)
    assert states["example-club"]["status"] == "provisioned"
    assert states["example-club"]["sent_at"] is None
    assert states["example-club"]["followup_due_at"] is None
    assert states["example-club"]["delete_due_at"] is None

    core.jobs.clear()
    status = client.get(
        f"/api/outreach/job/{payload['job_id']}",
        headers={"X-Admin-Secret": "test-secret"},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "provisioned"
    assert status.json()["persisted"] is True


def test_direct_request_is_idempotent_when_store_exists(monkeypatch, tmp_path):
    client, core, _states = _app_with_tracking(monkeypatch, tmp_path)
    core.existing = {"id": "gid://shopify/Metaobject/1"}
    response = client.post(
        "/api/outreach/storefront-request",
        headers={"X-Admin-Secret": "test-secret"},
        data=_valid_form(),
        files={"storefront_logo_file": ("logo.png", _logo_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "existing"
    assert core.provision_calls == []


def test_direct_request_rejects_logo_with_excess_padding(monkeypatch, tmp_path):
    client, core, _states = _app_with_tracking(monkeypatch, tmp_path)
    response = client.post(
        "/api/outreach/storefront-request",
        headers={"X-Admin-Secret": "test-secret"},
        data=_valid_form(),
        files={
            "storefront_logo_file": (
                "padded-logo.png",
                _logo_bytes(padded=True),
                "image/png",
            )
        },
    )
    assert response.status_code == 400
    assert "padding" in response.json()["error"]
    assert core.provision_calls == []


def test_mark_sent_starts_followup_and_delete_clocks(monkeypatch, tmp_path):
    client, _core, states = _app_with_tracking(monkeypatch, tmp_path)
    states["example-club"] = {
        "handle": "example-club",
        "status": "provisioned",
        "email_authorized": False,
    }
    response = client.post(
        "/api/outreach/store/example-club/mark-sent",
        headers={"X-Admin-Secret": "test-secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "outreach_sent"
    assert payload["sent_at"]
    assert payload["followup_due_at"]
    assert payload["delete_due_at"]
    assert states["example-club"]["email_authorized"] is True
