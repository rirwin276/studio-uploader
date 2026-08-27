from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import anonymous_demo_retention


class FakeCore:
    def __init__(self, owner=""):
        self.owner = owner
        self.jobs = {}
        self.deleted = []

    def _get_custom_shop(self, handle):
        return {"fields": {"owner_customer_id": self.owner}}

    def _normalize_store_owner(self, value):
        return "" if str(value).lower() == "unclaimed" else str(value)

    def _store_claim_lock(self, _handle):
        return threading.Lock()

    def _job_set(self, job_id, **patch):
        self.jobs.setdefault(job_id, {}).update(patch)

    def _job_get(self, job_id):
        return dict(self.jobs.get(job_id, {}))

    def _run_shopify_deprovision_job(self, job_id, handle):
        self.deleted.append(handle)
        self._job_set(job_id, status="done")


def _state(source="anonymous_demo", *, claimed=False, hours_ago=1):
    return {
        "source": source,
        "store_status": "anonymous_demo_unclaimed",
        "claim_status": "claimed" if claimed else "unclaimed",
        "status": "ready",
        "expires_at": (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
    }


def _wire(monkeypatch, states):
    monkeypatch.setattr(anonymous_demo_retention.outreach_tracking, "list_all", lambda _core: states)

    def update(_core, handle, patch):
        states[handle].update(patch)
        return states[handle]

    monkeypatch.setattr(anonymous_demo_retention.outreach_tracking, "update", update)


def test_only_expired_anonymous_demos_are_selected(monkeypatch):
    states = {
        "expired": _state(),
        "claimed": _state(claimed=True),
        "outreach": _state(source="direct_outreach_api"),
        "future": {
            **_state(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    }
    _wire(monkeypatch, states)
    assert anonymous_demo_retention.due_now(FakeCore()) == ["expired"]


def test_expired_unclaimed_demo_is_deleted_and_token_revoked(monkeypatch):
    states = {"expired": {**_state(), "resume_token_hash": "secret-hash"}}
    _wire(monkeypatch, states)
    core = FakeCore(owner="unclaimed")
    result = anonymous_demo_retention.process_due(core)
    assert result["deleted"] == ["expired"]
    assert core.deleted == ["expired"]
    assert states["expired"]["status"] == "deleted"
    assert states["expired"]["resume_token_hash"] is None


def test_shopify_owner_wins_and_cancels_deletion(monkeypatch):
    states = {"expired": _state()}
    _wire(monkeypatch, states)
    core = FakeCore(owner="12345")
    result = anonymous_demo_retention.process_due(core)
    assert result["deleted"] == []
    assert core.deleted == []
    assert states["expired"]["claim_status"] == "claimed"
    assert states["expired"]["expires_at"] is None


def test_owner_lookup_failure_fails_closed(monkeypatch):
    states = {"expired": _state()}
    _wire(monkeypatch, states)

    class Broken(FakeCore):
        def _get_custom_shop(self, _handle):
            raise RuntimeError("Shopify unavailable")

    core = Broken()
    result = anonymous_demo_retention.process_due(core)
    assert result["deleted"] == []
    assert core.deleted == []

