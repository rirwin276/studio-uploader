from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import outreach_manifest


def _write_request(root: Path, *, handle: str = "sample-rowing-club") -> Path:
    (root / "pending").mkdir(parents=True)
    (root / "logos").mkdir(parents=True)

    buf = BytesIO()
    Image.new("RGBA", (20, 10), (35, 35, 35, 255)).save(buf, format="PNG")
    (root / "logos" / "sample.png.b64").write_text(
        base64.b64encode(buf.getvalue()).decode("ascii"),
        encoding="ascii",
    )

    payload = {
        "enabled": True,
        "claimable": True,
        "run_id": "sample-20260819",
        "storefront_name": "Sample Rowing Club Store",
        "storefront_handle": handle,
        "contact_email": "info@example.org",
        "type_of_store": "rowing",
        "primary_color": "Charcoal",
        "logo_base64_file": "logos/sample.png.b64",
        "logo_preparation": {"target_width": 40},
    }
    path = root / "pending" / "sample.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeCore(SimpleNamespace):
    def __init__(self, upload_dir: Path, existing=None):
        super().__init__(
            UPLOAD_DIR=upload_dir,
            MAX_UPLOAD_BYTES=1024 * 1024,
            MAX_IMAGE_PIXELS=1_000_000,
        )
        self.existing = existing
        self.jobs = {}
        self.provision_calls = []
        self.deprovision_calls = []

    def _job_set(self, job_id, **values):
        self.jobs.setdefault(job_id, {}).update(values)

    def _get_custom_shop(self, handle):
        return self.existing

    def _run_shopify_provision_job(self, *args):
        self.provision_calls.append(args)
        self._job_set(args[0], status="succeeded")

    def _run_shopify_deprovision_job(self, *args):
        self.deprovision_calls.append(args)
        self._job_set(args[0], status="done")


def test_claimable_manifest_runs_ownerless_provision(tmp_path):
    root = tmp_path / "outreach"
    _write_request(root)
    core = FakeCore(tmp_path / "uploads")

    assert outreach_manifest.process_pending_manifests(core, root) == 1

    assert len(core.provision_calls) == 1
    call = core.provision_calls[0]
    assert call[0] == "outreach-sample-20260819"
    assert call[1] == "Sample Rowing Club Store"
    assert call[2] == "sample-rowing-club"
    assert call[3] == ""
    assert call[4] == "rowing"
    assert call[5] == "Charcoal"
    output = core.UPLOAD_DIR / f"{call[6]}_curr.png"
    assert output.is_file()
    with Image.open(output) as image:
        assert image.size == (40, 20)
    assert core.jobs[call[0]]["status"] == "succeeded"


def test_existing_handle_is_not_rebuilt(tmp_path):
    root = tmp_path / "outreach"
    _write_request(root)
    core = FakeCore(tmp_path / "uploads", existing={"id": "gid://shopify/Metaobject/1"})

    outreach_manifest.process_pending_manifests(core, root)

    assert core.provision_calls == []
    assert core.jobs["outreach-sample-20260819"]["status"] == "skipped_existing"


def test_manifest_must_explicitly_be_claimable(tmp_path):
    root = tmp_path / "outreach"
    path = _write_request(root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["claimable"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    core = FakeCore(tmp_path / "uploads")

    outreach_manifest.process_pending_manifests(core, root)

    assert core.provision_calls == []
    invalid_jobs = [job for key, job in core.jobs.items() if key.startswith("outreach-invalid-")]
    assert len(invalid_jobs) == 1
    assert invalid_jobs[0]["status"] == "failed"
    assert "claimable=true" in invalid_jobs[0]["error"]


def test_logo_path_cannot_escape_outreach_root(tmp_path):
    root = tmp_path / "outreach"
    path = _write_request(root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["logo_base64_file"] = "../outside.png.b64"
    path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        outreach_manifest._load_manifest(path, root)
    except outreach_manifest.OutreachManifestError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("path traversal should be rejected")


def test_connected_background_cleanup_preserves_enclosed_white(tmp_path):
    image = Image.new("RGB", (9, 9), "white")
    pixels = image.load()
    for x in range(2, 7):
        for y in range(2, 7):
            pixels[x, y] = (0, 0, 0)
    for x in range(3, 6):
        for y in range(3, 6):
            pixels[x, y] = (255, 255, 255)

    cleaned = outreach_manifest._remove_connected_neutral_background(image)

    assert cleaned.size == (5, 5)
    assert cleaned.getpixel((2, 2)) == (255, 255, 255, 255)
    assert cleaned.getpixel((0, 0))[3] == 255


def test_retire_manifest_requires_exact_confirmation_and_deletes_one_store(tmp_path):
    root = tmp_path / "outreach"
    (root / "retire").mkdir(parents=True)
    path = root / "retire" / "sample.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "action": "delete_store",
                "run_id": "sample-delete-20260819",
                "storefront_handle": "sample-rowing-club",
                "confirm_handle": "sample-rowing-club",
                "reason": "Rejected proof of concept",
            }
        ),
        encoding="utf-8",
    )
    core = FakeCore(tmp_path / "uploads")

    assert outreach_manifest.process_retire_manifests(core, root) == 1

    assert core.deprovision_calls == [
        ("outreach-retire-sample-delete-20260819", "sample-rowing-club")
    ]
    assert core.jobs["outreach-retire-sample-delete-20260819"]["status"] == "done"

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["confirm_handle"] = "wrong-store"
    path.write_text(json.dumps(payload), encoding="utf-8")
    core = FakeCore(tmp_path / "uploads")
    outreach_manifest.process_retire_manifests(core, root)
    assert core.deprovision_calls == []


def test_retired_handle_cannot_rebuild_in_same_deployment(tmp_path):
    root = tmp_path / "outreach"
    _write_request(root)
    core = FakeCore(tmp_path / "uploads")

    assert outreach_manifest.process_pending_manifests(
        core,
        root,
        blocked_handles={"sample-rowing-club"},
    ) == 1

    assert core.provision_calls == []
    job = core.jobs["outreach-sample-20260819"]
    assert job["status"] == "skipped_retired"
    assert "no rebuild" in job["reason"]
