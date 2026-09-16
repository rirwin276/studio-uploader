from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import anonymous_demo_retention

NOW = datetime(2026, 9, 16, 10, 15, tzinfo=timezone.utc)


class FakeCore:
    def __init__(self, owner=""):
        self.owner = owner
        self.jobs = {}
        self.deleted = []
        self.marker = ""

    def _get_custom_shop(self, handle):
        return {"fields": {"owner_customer_id": self.owner, "collection_gid": "collection-1"}}

    def _get_collection_claim_owner(self, _collection):
        return self.marker

    def _try_create_collection_claim(self, _collection, owner):
        if self.marker:
            return False
        self.marker = owner
        return True

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
        "expires_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
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
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    }
    _wire(monkeypatch, states)
    assert anonymous_demo_retention.due_now(FakeCore(), now=NOW.timestamp()) == ["expired"]


def test_expired_unclaimed_demo_is_deleted_and_token_revoked(monkeypatch):
    states = {"expired": {**_state(), "resume_token_hash": "secret-hash"}}
    _wire(monkeypatch, states)
    core = FakeCore(owner="unclaimed")
    result = anonymous_demo_retention.process_due(core, now=NOW.timestamp())
    assert result["deleted"] == ["expired"]
    assert core.deleted == ["expired"]
    assert states["expired"]["status"] == "deleted"
    assert states["expired"]["resume_token_hash"] is None


def test_shopify_owner_wins_and_cancels_deletion(monkeypatch):
    states = {"expired": _state()}
    _wire(monkeypatch, states)
    core = FakeCore(owner="12345")
    result = anonymous_demo_retention.process_due(core, now=NOW.timestamp())
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
    result = anonymous_demo_retention.process_due(core, now=NOW.timestamp())
    assert result["deleted"] == []
    assert core.deleted == []



def test_never_deletes_during_daytime(monkeypatch):
    states = {'expired': _state(hours_ago=72)}
    _wire(monkeypatch, states)
    core = FakeCore()
    noon = datetime(2026,9,16,19,tzinfo=timezone.utc)
    assert anonymous_demo_retention.process_due(core, now=noon.timestamp())['deleted'] == []
    assert core.deleted == []


def test_deadline_is_never_before_48_hours_and_tracks_dst():
    for raw, expected in [
        ('2026-09-16T09:59:00+00:00','2026-09-16T10:00:00+00:00'),
        ('2026-09-16T10:01:00+00:00','2026-09-17T10:00:00+00:00'),
        ('2026-11-01T08:30:00+00:00','2026-11-01T11:00:00+00:00'),
        ('2026-03-08T09:30:00+00:00','2026-03-08T10:00:00+00:00'),
    ]:
        expiry = datetime.fromisoformat(raw)
        due = anonymous_demo_retention.overnight_deadline(expiry)
        assert due.isoformat() == expected
        assert due >= expiry


def test_atomic_claim_marker_prevents_deletion_before_owner_sync(monkeypatch):
    states = {'expired': _state()}
    _wire(monkeypatch, states)
    core = FakeCore()
    core._get_custom_shop = lambda _: {'fields':{'owner_customer_id':'unclaimed','collection_gid':'collection-1'}}
    core._get_collection_claim_owner = lambda _: '12345'
    assert anonymous_demo_retention.process_due(core, now=NOW.timestamp())['deleted'] == []
    assert states['expired']['claim_status'] == 'claimed'


def test_customer_winning_cross_process_cas_cancels_cleanup(monkeypatch):
    states = {'expired': _state()}
    _wire(monkeypatch, states)
    core = FakeCore()
    def customer_wins(_collection, _marker):
        core.marker = 'customer-42'
        return False
    core._try_create_collection_claim = customer_wins
    assert anonymous_demo_retention.process_due(core, now=NOW.timestamp())['deleted'] == []
    assert core.deleted == []


def test_cleanup_reserves_same_atomic_marker_before_removing(monkeypatch):
    from anonymous_lifecycle import DELETION_CLAIM
    states = {'expired': _state()}
    _wire(monkeypatch, states)
    core = FakeCore()
    original = core._run_shopify_deprovision_job
    def delete(job, handle):
        assert core.marker == DELETION_CLAIM
        assert not core._try_create_collection_claim('collection-1', 'customer-42')
        original(job, handle)
    core._run_shopify_deprovision_job = delete
    assert anonymous_demo_retention.process_due(core, now=NOW.timestamp())['deleted'] == ['expired']


def test_failed_cleanup_keeps_claim_block_and_can_retry(monkeypatch):
    from anonymous_lifecycle import DELETION_CLAIM
    states = {'expired': _state()}
    _wire(monkeypatch, states)
    core = FakeCore()
    original = core._run_shopify_deprovision_job
    core._run_shopify_deprovision_job = lambda job, handle: core._job_set(job, status='error')
    assert anonymous_demo_retention.process_due(core, now=NOW.timestamp())['deleted'] == []
    assert core.marker == DELETION_CLAIM
    core._run_shopify_deprovision_job = original
    assert anonymous_demo_retention.process_due(core, now=NOW.timestamp())['deleted'] == ['expired']
