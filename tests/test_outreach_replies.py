from __future__ import annotations

import pytest

import outreach_mail
import outreach_replies


class FakeCore:
    def __init__(self):
        self.jobs = {}
        self.deprovisioned = []

    def _job_set(self, job_id, **fields):
        self.jobs.setdefault(job_id, {}).update(fields)

    def _run_shopify_deprovision_job(self, job_id, handle):
        self.deprovisioned.append(handle)


@pytest.fixture
def wired(monkeypatch):
    states = {
        "westside-rowing": {
            "handle": "westside-rowing",
            "source": "vendor_neutral_outreach_intake",
            "storefront_name": "Westside Rowing Team Store",
            "contact_email": "info@westside.org",
            "status": "outreach_sent",
            "sent_at": "2026-08-22T09:00:00+00:00",
            "followup_due_at": "2026-08-25T09:00:00+00:00",
            "delete_due_at": "2026-08-29T09:00:00+00:00",
        }
    }
    forwarded = []

    monkeypatch.setattr(outreach_replies.outreach_tracking, "list_all", lambda _c: dict(states))
    monkeypatch.setattr(outreach_replies.outreach_tracking, "read", lambda _c, h: dict(states.get(h, {})))

    def update(_core, handle, patch):
        states[handle] = {**states.get(handle, {}), **patch}
        return dict(states[handle])

    monkeypatch.setattr(outreach_replies.outreach_tracking, "update", update)
    monkeypatch.setattr(outreach_mail, "configured", lambda: True)
    monkeypatch.setattr(outreach_mail, "send", lambda m: forwarded.append(m))
    monkeypatch.setenv("OUTREACH_REPLY_TO", "ryan@example.com")
    monkeypatch.setattr(
        outreach_replies.threading, "Thread",
        lambda target, args=(), **kw: type("T", (), {"start": lambda _s: target(*args)})(),
    )
    return FakeCore(), states, forwarded


def _inbound(body, sender="info@westside.org", subject="Re: Team store idea"):
    return {"from": sender, "to": "replies@outreach.stellasageco.com",
            "subject": subject, "text": body}


def test_a_plain_refusal_deletes_the_store(wired):
    core, states, forwarded = wired
    result = outreach_replies.handle_reply(core, _inbound("No thanks, please remove us."))

    assert result["decision"] == "opt_out"
    assert core.deprovisioned == ["westside-rowing"]
    assert states["westside-rowing"]["status"] == "declined"
    # A follow-up to somebody who just asked to be left alone is the worst
    # possible next email.
    assert states["westside-rowing"]["followup_due_at"] is None
    assert states["westside-rowing"]["delete_due_at"] is None
    assert len(forwarded) == 1


def test_a_question_is_never_answered_by_the_machine(wired):
    core, states, forwarded = wired
    result = outreach_replies.handle_reply(
        core, _inbound("This looks interesting - how much does shipping cost?")
    )

    assert result["decision"] == "question"
    assert core.deprovisioned == []
    assert states["westside-rowing"]["needs_human_reply"] is True
    assert len(forwarded) == 1


def test_a_refusal_with_a_question_in_it_is_a_question(wired):
    """"Not interested unless you do youth sizes — can you?" is somebody asking
    to keep the store, and deleting it would be the wrong half of the sentence.
    """
    core, _states, _forwarded = wired
    result = outreach_replies.handle_reply(
        core, _inbound("Not interested unless you can do youth sizes. Can you?")
    )

    assert result["decision"] == "question"
    assert core.deprovisioned == []


def test_our_own_words_quoted_back_do_not_delete_the_store(wired):
    """Every outreach email says "reply no thanks and I'll delete it". Quoted
    into a reply, reading the whole thread finds an opt-out inside a friendly
    message."""
    core, _states, _forwarded = wired
    body = (
        "Thanks for putting this together, we will talk about it Tuesday. "
        "On Aug 22 Ryan Irwin wrote: If it is not useful, just reply "
        "\"no thanks\" and I'll delete the whole thing."
    )
    result = outreach_replies.handle_reply(core, _inbound(body))

    assert result["decision"] != "opt_out"
    assert core.deprovisioned == []


def test_an_ambiguous_reply_waits_for_a_person(wired):
    core, states, _forwarded = wired
    result = outreach_replies.handle_reply(core, _inbound("Passing this to our board."))

    assert result["decision"] == "unclear"
    assert core.deprovisioned == []
    assert states["westside-rowing"]["needs_human_reply"] is True


def test_a_reply_from_a_colleague_still_finds_the_store(wired):
    core, _states, _forwarded = wired
    result = outreach_replies.handle_reply(
        core, _inbound("Who is this?", sender="treasurer@westside.org")
    )
    assert result["handle"] == "westside-rowing"


def test_an_unmatched_reply_is_still_forwarded(wired):
    """Somebody wrote to us. Not knowing which store it is about is not a
    reason for nobody to read it."""
    core, _states, forwarded = wired
    result = outreach_replies.handle_reply(
        core, _inbound("Hello?", sender="stranger@nowhere.test", subject="hi")
    )

    assert result["matched"] is False
    assert core.deprovisioned == []
    assert len(forwarded) == 1


def test_an_html_only_reply_is_still_read(wired):
    core, _states, _forwarded = wired
    payload = {
        "from": "info@westside.org",
        "to": "replies@outreach.stellasageco.com",
        "subject": "Re: Team store",
        "html": "<div><p>Please remove us.</p></div>",
    }
    assert outreach_replies.handle_reply(core, payload)["decision"] == "opt_out"


def test_the_payload_shape_is_not_assumed(wired):
    """Inbound webhook shapes differ between providers and between versions of
    the same one. A reply lost to a renamed key is a reply nobody answers."""
    core, _states, _forwarded = wired
    nested = {"data": {"from": [{"address": "info@westside.org"}],
                       "subject": "Re: store", "text": "no thanks"}}
    result = outreach_replies.handle_reply(core, nested)

    assert result["handle"] == "westside-rowing"
    assert result["decision"] == "opt_out"


def test_replies_awaiting_an_answer_are_listed(wired):
    core, _states, _forwarded = wired
    outreach_replies.handle_reply(core, _inbound("Can you do hats?"))
    pending = outreach_replies.pending_replies(core)

    assert len(pending) == 1
    assert pending[0]["handle"] == "westside-rowing"
    assert pending[0]["decision"] == "question"


def test_a_handled_opt_out_does_not_sit_in_the_queue(wired):
    core, _states, _forwarded = wired
    outreach_replies.handle_reply(core, _inbound("Unsubscribe"))
    assert outreach_replies.pending_replies(core) == []


def test_answering_clears_the_queue(wired):
    """The answer is written in a mail client this never sees, so the queue can
    only empty when a person says it is dealt with."""
    core, states, _forwarded = wired
    outreach_replies.handle_reply(core, _inbound("Can you do hats?"))

    result = outreach_replies.mark_answered(core, "westside-rowing")

    assert result["cleared"] == 1
    assert states["westside-rowing"]["needs_human_reply"] is False
    assert outreach_replies.pending_replies(core) == []


def test_answering_an_unknown_store_is_not_silently_ignored(wired):
    core, _states, _forwarded = wired
    with pytest.raises(LookupError):
        outreach_replies.mark_answered(core, "no-such-store")
