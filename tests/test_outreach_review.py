from __future__ import annotations

import pytest

import outreach_mail
import outreach_review


class FakeCore:
    def __init__(self):
        self.jobs = {}
        self.deprovisioned = []

    def _job_set(self, job_id, **fields):
        self.jobs.setdefault(job_id, {}).update(fields)

    def _run_shopify_deprovision_job(self, job_id, handle):
        self.deprovisioned.append((job_id, handle))


def _states():
    return {
        "westside-rowing": {
            "handle": "westside-rowing",
            "source": "vendor_neutral_outreach_intake",
            "storefront_name": "Westside Rowing Team Store",
            "contact_email": "info@westside.org",
            "status": "provisioned",
            "built_at": "2026-08-22T02:10:00+00:00",
            "sent_at": None,
        }
    }


@pytest.fixture
def wired(monkeypatch):
    states = _states()
    sent = []

    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(states))
    monkeypatch.setattr(outreach_review.outreach_tracking, "read", lambda _c, h: dict(states.get(h, {})))

    def update(_core, handle, patch):
        states[handle] = {**states.get(handle, {}), **patch}
        return dict(states[handle])

    monkeypatch.setattr(outreach_review.outreach_tracking, "update", update)
    monkeypatch.setattr(outreach_mail, "configured", lambda: True)
    monkeypatch.setattr(outreach_mail, "send", lambda message: sent.append(message))
    # Threads would outlive the test; run the nuke inline instead.
    monkeypatch.setattr(
        outreach_review.threading,
        "Thread",
        lambda target, args=(), **kw: type("T", (), {"start": lambda _s: target(*args)})(),
    )
    return FakeCore(), states, sent


def test_a_built_store_waits_in_the_queue(wired):
    core, _states, _sent = wired
    queue = outreach_review.pending_queue(core)
    assert [row["handle"] for row in queue] == ["westside-rowing"]
    # The greeting has to read like a person wrote it, not a mail merge.
    assert queue[0]["organization"] == "Westside Rowing"


def test_accept_sends_then_starts_the_clock(wired):
    core, states, sent = wired
    result = outreach_review.accept(core, "westside-rowing")

    assert len(sent) == 1
    assert sent[0]["To"] == "info@westside.org"
    assert result["status"] == "outreach_sent"
    assert states["westside-rowing"]["email_authorized"] is True
    assert result["followup_due_at"] and result["delete_due_at"]
    # Decided, so it leaves the queue.
    assert outreach_review.pending_queue(core) == []


def test_a_failed_send_leaves_the_store_in_the_queue(wired, monkeypatch):
    """No email means no clock.

    Stamping the dates before the send would leave a store nobody was contacted
    about counting down toward a follow-up to an email that never arrived, and
    toward deletion for not replying to it.
    """
    core, states, _sent = wired

    def boom(_message):
        raise RuntimeError("smtp refused")

    monkeypatch.setattr(outreach_mail, "send", boom)

    with pytest.raises(RuntimeError):
        outreach_review.accept(core, "westside-rowing")

    assert states["westside-rowing"].get("sent_at") is None
    assert states["westside-rowing"].get("delete_due_at") is None
    assert [row["handle"] for row in outreach_review.pending_queue(core)] == ["westside-rowing"]


def test_decline_deletes_the_store_and_emails_nobody(wired):
    core, states, sent = wired
    result = outreach_review.decline(core, "westside-rowing", reason="already has a shop")

    assert sent == []
    assert result["status"] == "declined"
    assert core.deprovisioned == [(result["job_id"], "westside-rowing")]
    assert states["westside-rowing"]["review_note"] == "already has a shop"
    # A declined store must never later look due for a follow-up or a deletion.
    assert states["westside-rowing"]["followup_due_at"] is None
    assert states["westside-rowing"]["delete_due_at"] is None
    assert outreach_review.pending_queue(core) == []


def test_a_decided_store_cannot_be_decided_twice(wired):
    core, _states, _sent = wired
    outreach_review.accept(core, "westside-rowing")
    with pytest.raises(PermissionError):
        outreach_review.accept(core, "westside-rowing")
    with pytest.raises(PermissionError):
        outreach_review.decline(core, "westside-rowing")


def test_accept_refuses_when_email_is_not_configured(wired, monkeypatch):
    core, states, sent = wired
    monkeypatch.setattr(outreach_mail, "configured", lambda: False)
    with pytest.raises(RuntimeError):
        outreach_review.accept(core, "westside-rowing")
    assert sent == []
    assert states["westside-rowing"].get("sent_at") is None


def test_a_website_store_never_enters_the_review_queue(wired):
    core, states, _sent = wired
    states["westside-rowing"]["source"] = "website_request_form"
    assert outreach_review.pending_queue(core) == []


def _store(handle, **overrides):
    row = {
        "handle": handle,
        "source": "vendor_neutral_outreach_intake",
        "storefront_name": handle.replace("-", " ").title() + " Team Store",
        "contact_email": f"info@{handle}.org",
        "organization_url": f"https://{handle}.org/",
        "status": "provisioned",
        "built_at": "2026-08-22T02:00:00+00:00",
    }
    row.update(overrides)
    return row


def _pipeline_core(monkeypatch, stores):
    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(stores))
    return FakeCore()


def test_the_pipeline_sorts_every_store_into_one_stage(monkeypatch):
    core = _pipeline_core(monkeypatch, {
        "waiting": _store("waiting"),
        "emailed": _store("emailed", sent_at="2026-08-22T09:00:00+00:00", status="outreach_sent"),
        "won": _store("won", sent_at="2026-08-20T09:00:00+00:00", claim_status="claimed",
                      claimed_at="2026-08-21T09:00:00+00:00"),
        "gone": _store("gone", declined_at="2026-08-22T10:00:00+00:00", status="declined"),
        "broken": _store("broken", status="intake_failed"),
        "mid": _store("mid", status="building"),
    })
    result = outreach_review.pipeline(core)

    assert [r["handle"] for r in result["pending"]] == ["waiting"]
    assert [r["handle"] for r in result["sent"]] == ["emailed"]
    assert [r["handle"] for r in result["claimed"]] == ["won"]
    assert [r["handle"] for r in result["failed"]] == ["broken"]
    assert [r["handle"] for r in result["building"]] == ["mid"]


def test_a_claimed_store_counts_as_claimed_not_as_emailed_only(monkeypatch):
    """Claimed is the outcome, so it wins over every earlier stage the store
    also passed through."""
    core = _pipeline_core(monkeypatch, {
        "won": _store("won", sent_at="2026-08-20T09:00:00+00:00", claim_status="claimed"),
    })
    result = outreach_review.pipeline(core)
    assert result["totals"]["claimed"] == 1
    assert result["totals"]["emailed"] == 1
    assert result["pending"] == []


def test_the_totals_report_what_prospects_actually_did(monkeypatch):
    core = _pipeline_core(monkeypatch, {
        "looked": _store("looked", sent_at="2026-08-22T09:00:00+00:00", prospect_demo={
            "event_counts": {"prospect_store_opened": 3},
        }),
        "played": _store("played", sent_at="2026-08-22T09:00:00+00:00", prospect_demo={
            "event_counts": {
                "prospect_store_opened": 1,
                "admin_demo_opened": 2,
                "demo_product_successfully_created": 1,
            },
        }),
        "ignored": _store("ignored", sent_at="2026-08-22T09:00:00+00:00"),
    })
    totals = outreach_review.pipeline(core)["totals"]

    assert totals["emailed"] == 3
    assert totals["visited"] == 2
    assert totals["tried_admin"] == 1
    assert totals["made_product"] == 1


def test_a_declined_store_never_inflates_the_funnel(monkeypatch):
    core = _pipeline_core(monkeypatch, {
        "gone": _store("gone", declined_at="2026-08-22T10:00:00+00:00", status="declined",
                       sent_at="2026-08-22T09:00:00+00:00"),
    })
    totals = outreach_review.pipeline(core)["totals"]
    assert totals["declined"] == 1
    assert totals["built"] == 0
    assert totals["emailed"] == 0


def test_a_website_store_is_not_in_the_outreach_funnel(monkeypatch):
    core = _pipeline_core(monkeypatch, {
        "customer": _store("customer", source="website_request_form"),
    })
    result = outreach_review.pipeline(core)
    assert result["totals"]["found"] == 0
    assert result["pending"] == []


class JobCore(FakeCore):
    def __init__(self, jobs=None):
        super().__init__()
        self._jobs = jobs or {}

    def _job_get(self, job_id):
        return self._jobs.get(job_id, {})


def test_a_failed_store_shows_why_it_failed(monkeypatch):
    """The reason was always recorded and never shown, which left the only
    honest answer to 'did it work' as 'open Railway and read the logs'."""
    stores = {
        "oranco": _store("oranco", status="intake_failed",
                         intake_error="logo_source_url did not return an image",
                         failed_at="2026-08-22T22:45:31+00:00"),
    }
    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(stores))
    row = outreach_review.pipeline(FakeCore())["failed"][0]

    assert row["error"] == "logo_source_url did not return an image"


def test_the_steps_say_where_a_store_stopped(monkeypatch):
    stores = {
        "oranco": _store("oranco", status="intake_failed",
                         created_at="2026-08-22T22:45:00+00:00",
                         build_started_at=None, built_at=None,
                         failed_at="2026-08-22T22:45:31+00:00"),
    }
    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(stores))
    steps = outreach_review.pipeline(FakeCore())["failed"][0]["steps"]
    done = {step["label"]: step["done"] for step in steps}

    assert done["Queued"] is True
    assert done["Logo prepared, build started"] is False
    assert done["Store and products built"] is False
    assert done["Stopped"] is True


def test_the_provisioner_log_tail_comes_back_with_a_stuck_store(monkeypatch):
    stores = {"mid": _store("mid", status="building", job_id="job-1")}
    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(stores))
    core = JobCore({"job-1": {
        "status": "running",
        "stdout": "\n".join("line %d" % n for n in range(1, 40)),
    }})
    row = outreach_review.pipeline(core)["building"][0]

    assert row["log"][-1] == "line 39"
    assert len(row["log"]) == 12  # tail only; the useful part is the bottom


def test_a_settled_store_does_not_go_asking_about_old_jobs(monkeypatch):
    """A sent store's provisioning finished days ago, and reading it back on
    every page load costs a lookup per store for nothing."""
    stores = {"emailed": _store("emailed", sent_at="2026-08-22T09:00:00+00:00", job_id="job-1")}
    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(stores))

    asked = []

    class Counting(JobCore):
        def _job_get(self, job_id):
            asked.append(job_id)
            return {}

    outreach_review.pipeline(Counting())
    assert asked == []


# ---- retrying a failure ---------------------------------------------------


def _failed_core(monkeypatch, stores):
    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(stores))
    monkeypatch.setattr(
        outreach_review.outreach_tracking, "read", lambda _c, h: dict(stores.get(h, {}))
    )

    def update(_core, handle, patch):
        stores[handle] = {**stores.get(handle, {}), **patch}
        return dict(stores[handle])

    monkeypatch.setattr(outreach_review.outreach_tracking, "update", update)
    return FakeCore()


def test_a_failed_store_can_be_put_back_in_the_queue(monkeypatch):
    """A failure is otherwise permanent in both directions: the worker only
    picks up queued work, and discovery avoids every domain already in the
    ledger. Nothing would ever reach this organization again."""
    stores = {"oranco": _store("oranco", status="intake_failed",
                               intake_error="logo source must be at least 256px wide",
                               failed_at="2026-08-22T22:45:31+00:00")}
    core = _failed_core(monkeypatch, stores)

    result = outreach_review.retry(core, "oranco")

    assert result["status"] == "intake_queued"
    assert stores["oranco"]["status"] == "intake_queued"
    assert stores["oranco"]["intake_error"] is None
    assert stores["oranco"]["failed_at"] is None


def test_a_live_store_is_never_requeued(monkeypatch):
    """Requeuing a built store would build a second one over the top of it."""
    stores = {"westside": _store("westside", status="provisioned")}
    core = _failed_core(monkeypatch, stores)

    with pytest.raises(PermissionError):
        outreach_review.retry(core, "westside")
    assert stores["westside"]["status"] == "provisioned"


def test_an_emailed_store_is_never_requeued(monkeypatch):
    stores = {"westside": _store("westside", status="outreach_sent",
                                 sent_at="2026-08-22T09:00:00+00:00")}
    core = _failed_core(monkeypatch, stores)
    with pytest.raises(PermissionError):
        outreach_review.retry(core, "westside")


def test_a_website_store_is_not_retried_from_here(monkeypatch):
    stores = {"customer": _store("customer", source="website_request_form",
                                 status="intake_failed")}
    core = _failed_core(monkeypatch, stores)
    with pytest.raises(PermissionError):
        outreach_review.retry(core, "customer")


def test_retrying_something_that_does_not_exist_says_so(monkeypatch):
    core = _failed_core(monkeypatch, {})
    with pytest.raises(LookupError):
        outreach_review.retry(core, "nobody")


# ---- removing a store outright --------------------------------------------


def test_a_wrong_store_can_be_deleted_at_any_stage(monkeypatch):
    """Decline is the reviewer's "no" on a store still waiting. This is the
    other thing: a store that should not exist, whatever stage it reached."""
    stores = {"cologne": _store("cologne", status="outreach_sent",
                                sent_at="2026-08-23T09:00:00+00:00")}
    core = _failed_core(monkeypatch, stores)

    result = outreach_review.remove(core, "cologne", reason="wrong club's logo")

    assert result["status"] == "declined"
    assert core.deprovisioned == [(result["job_id"], "cologne")]
    assert stores["cologne"]["review_decision"] == "removed"
    assert stores["cologne"]["review_note"] == "wrong club's logo"
    # It must never look due for a follow-up or a scheduled deletion again.
    assert stores["cologne"]["followup_due_at"] is None
    assert stores["cologne"]["delete_due_at"] is None


def test_a_failed_store_can_be_deleted_too(monkeypatch):
    stores = {"oranco": _store("oranco", status="intake_failed")}
    core = _failed_core(monkeypatch, stores)
    outreach_review.remove(core, "oranco")
    assert core.deprovisioned


def test_a_claimed_store_is_never_deleted_from_here(monkeypatch):
    """It belongs to somebody now. Deleting it would take a real customer's
    storefront out from under them."""
    stores = {"westside": _store("westside", claim_status="claimed",
                                 claimed_at="2026-08-21T09:00:00+00:00")}
    core = _failed_core(monkeypatch, stores)

    with pytest.raises(PermissionError, match="claimed"):
        outreach_review.remove(core, "westside")
    assert core.deprovisioned == []


def test_a_website_store_is_not_deletable_from_the_outreach_page(monkeypatch):
    stores = {"customer": _store("customer", source="website_request_form")}
    core = _failed_core(monkeypatch, stores)
    with pytest.raises(PermissionError):
        outreach_review.remove(core, "customer")
    assert core.deprovisioned == []
