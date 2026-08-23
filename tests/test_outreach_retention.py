from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import outreach_retention


class FakeCore:
    """Mirrors app._run_shopify_deprovision_job, which reports through the job
    record rather than by raising."""

    def __init__(self, outcome="done", error=""):
        self.jobs = {}
        self.deprovisioned = []
        self.outcome = outcome
        self.error = error

    def _job_set(self, job_id, **fields):
        self.jobs.setdefault(job_id, {}).update(fields)

    def _job_get(self, job_id):
        return self.jobs.get(job_id, {})

    def _run_shopify_deprovision_job(self, job_id, handle):
        self.deprovisioned.append(handle)
        self._job_set(job_id, status=self.outcome, error=self.error)


def _iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _store(handle, *, counts=None, **overrides):
    row = {
        "handle": handle,
        "source": "vendor_neutral_outreach_intake",
        "storefront_name": f"{handle} Team Store",
        "status": "outreach_sent",
        "sent_at": _iso(-8),
        "delete_due_at": _iso(-1),
        "claim_status": "unclaimed",
    }
    if counts is not None:
        row["prospect_demo"] = {"event_counts": counts}
    row.update(overrides)
    return row


@pytest.fixture
def wired(monkeypatch):
    stores = {}

    monkeypatch.setattr(outreach_retention.outreach_tracking, "list_all", lambda _c: dict(stores))

    def update(_core, handle, patch):
        stores[handle] = {**stores.get(handle, {}), **patch}
        return dict(stores[handle])

    monkeypatch.setattr(outreach_retention.outreach_tracking, "update", update)
    monkeypatch.setattr(
        outreach_retention.threading, "Thread",
        lambda target, args=(), **kw: type("T", (), {"start": lambda _s: target(*args)})(),
    )
    return FakeCore(), stores


def test_an_untouched_store_is_taken_down_as_promised(wired):
    """The email says "I'll take it down in about a week". Nothing ever did."""
    core, stores = wired
    stores["quiet"] = _store("quiet")

    result = outreach_retention.process_due(core)

    assert result["deleted"] == ["quiet"]
    assert core.deprovisioned == ["quiet"]
    assert stores["quiet"]["status"] == "declined"
    assert stores["quiet"]["delete_due_at"] is None


def test_a_store_someone_played_with_is_kept(wired):
    """Deleting the store of somebody who opened the admin and built a product
    is the rudest possible answer to interest."""
    core, stores = wired
    stores["keen"] = _store("keen", counts={
        "admin_demo_opened": 2, "demo_product_successfully_created": 1,
    })

    result = outreach_retention.process_due(core)

    assert result["deleted"] == []
    assert result["kept"] == ["keen"]
    assert core.deprovisioned == []


def test_merely_opening_the_store_is_not_engagement(wired):
    """An email client prefetching the link would qualify, and so would the
    sender checking their own work."""
    core, stores = wired
    stores["glanced"] = _store("glanced", counts={"prospect_store_opened": 3})

    assert outreach_retention.process_due(core)["deleted"] == ["glanced"]


def test_a_store_the_founder_was_testing_is_not_mistaken_for_interest(wired):
    """Staff events are never counted, so a store its author poked at neither
    looks engaged nor gets spared on the strength of it."""
    core, stores = wired
    # Staff activity leaves rows behind but never increments the counters.
    stores["mine"] = _store("mine", counts={}, prospect_demo={
        "event_counts": {},
        "events": [{"event": "admin_demo_opened", "staff": True}],
    })

    assert outreach_retention.process_due(core)["deleted"] == ["mine"]


def test_a_claimed_store_is_never_deleted(wired):
    core, stores = wired
    stores["theirs"] = _store("theirs", claim_status="claimed")

    assert outreach_retention.process_due(core)["deleted"] == []
    assert core.deprovisioned == []


def test_a_store_that_was_never_emailed_has_no_clock(wired):
    """The week the email promised never started."""
    core, stores = wired
    stores["waiting"] = _store("waiting", sent_at=None, status="provisioned")

    assert outreach_retention.process_due(core)["deleted"] == []


def test_a_store_still_inside_its_window_is_left_alone(wired):
    core, stores = wired
    stores["fresh"] = _store("fresh", sent_at=_iso(-2), delete_due_at=_iso(5))

    assert outreach_retention.process_due(core)["deleted"] == []


def test_a_website_customers_store_is_not_in_scope(wired):
    core, stores = wired
    stores["customer"] = _store("customer", source="website_request_form")

    assert outreach_retention.process_due(core)["deleted"] == []


def test_a_dry_run_decides_without_deleting(wired):
    core, stores = wired
    stores["quiet"] = _store("quiet")

    result = outreach_retention.process_due(core, dry_run=True)

    assert result["considered"] == 1
    assert result["deleted"] == []
    assert core.deprovisioned == []
    assert stores["quiet"]["status"] == "outreach_sent"


def test_it_stays_switched_off_until_it_is_switched_on(monkeypatch):
    monkeypatch.delenv("OUTREACH_RETENTION_ENABLED", raising=False)
    assert outreach_retention.enabled() is False
    monkeypatch.setenv("OUTREACH_RETENTION_ENABLED", "true")
    assert outreach_retention.enabled() is True


# ---- a deletion that did not happen must not be recorded as one ------------


def test_a_failed_nuke_leaves_the_record_alone(monkeypatch):
    """The deprovisioner treats every step as non-fatal, so a nuke where
    nothing succeeded still finishes. Recording that as deleted would leave a
    live store that nothing ever revisits — discovery avoids the domain
    because the record says it is gone."""
    stores = {"stubborn": _store("stubborn")}
    monkeypatch.setattr(outreach_retention.outreach_tracking, "list_all", lambda _c: dict(stores))

    def update(_core, handle, patch):
        stores[handle] = {**stores.get(handle, {}), **patch}
        return dict(stores[handle])

    monkeypatch.setattr(outreach_retention.outreach_tracking, "update", update)
    core = FakeCore(outcome="error", error="Deprovision failed (exit 1)")

    result = outreach_retention.process_due(core)

    assert result["deleted"] == []
    assert stores["stubborn"]["status"] == "outreach_sent"
    assert stores["stubborn"]["delete_due_at"] is not None


def test_the_next_pass_tries_a_failed_deletion_again(monkeypatch):
    """Deleting is idempotent, so leaving the row untouched is what makes the
    retry safe."""
    stores = {"stubborn": _store("stubborn")}
    monkeypatch.setattr(outreach_retention.outreach_tracking, "list_all", lambda _c: dict(stores))
    monkeypatch.setattr(
        outreach_retention.outreach_tracking, "update",
        lambda _c, h, patch: stores.__setitem__(h, {**stores[h], **patch}) or dict(stores[h]),
    )

    failing = FakeCore(outcome="error")
    outreach_retention.process_due(failing)
    assert failing.deprovisioned == ["stubborn"]

    succeeding = FakeCore(outcome="done")
    result = outreach_retention.process_due(succeeding)
    assert result["deleted"] == ["stubborn"]
    assert stores["stubborn"]["status"] == "declined"


def test_the_store_is_gone_before_the_record_says_so(monkeypatch):
    """Order matters: marking first is what allowed the record to outrun
    reality."""
    stores = {"quiet": _store("quiet")}
    order = []
    monkeypatch.setattr(outreach_retention.outreach_tracking, "list_all", lambda _c: dict(stores))
    monkeypatch.setattr(
        outreach_retention.outreach_tracking, "update",
        lambda _c, h, patch: (order.append("recorded"), stores.__setitem__(h, {**stores[h], **patch}))[1],
    )

    class Ordered(FakeCore):
        def _run_shopify_deprovision_job(self, job_id, handle):
            order.append("deleted")
            super()._run_shopify_deprovision_job(job_id, handle)

    outreach_retention.process_due(Ordered())
    assert order == ["deleted", "recorded"]
