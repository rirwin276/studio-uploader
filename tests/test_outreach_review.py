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
