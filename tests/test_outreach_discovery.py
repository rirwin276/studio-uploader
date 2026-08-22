from __future__ import annotations

import json

import pytest

import outreach_discovery


class FakeCore:
    pass


def _candidate(handle="westside-rowing", **overrides):
    row = {
        "storefront_name": "Westside Rowing Team Store",
        "storefront_handle": handle,
        "type_of_store": "rowing club",
        "primary_color": "Navy",
        "contact_email": "info@westside.org",
        "organization_url": "https://westside.org/",
        "contact_source_url": "https://westside.org/contact",
        "logo_source_url": "https://westside.org/logo.png",
        "why_it_qualifies": "No shop page under /store or /merch.",
    }
    row.update(overrides)
    return row


@pytest.fixture
def wired(monkeypatch):
    ledger = {}
    submitted = []

    monkeypatch.setattr(outreach_discovery.outreach_tracking, "list_all", lambda _c: dict(ledger))
    monkeypatch.setattr(outreach_discovery.outreach_tracking, "read", lambda _c, h: dict(ledger.get(h, {})))

    def upsert(_core, handle, state):
        ledger[handle] = dict(state)

    monkeypatch.setattr(outreach_discovery.outreach_tracking, "upsert", upsert)

    import outreach_intake

    def queue(_core, payload, normalized=False):
        submitted.append(payload)
        return {"status": "intake_queued", "storefront_handle": payload["storefront_handle"]}

    monkeypatch.setattr(outreach_intake, "queue_intake_payload", queue, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return FakeCore(), ledger, submitted


def _answer(monkeypatch, candidates):
    def ask(limit, avoid):
        return list(candidates), {"model": "test-model", "seconds": 0.1}

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", ask)


def test_a_dry_run_finds_candidates_and_creates_nothing(wired, monkeypatch):
    """The whole point of a dry run: see what the brief returns before it is
    allowed to make a real Shopify store."""
    core, ledger, submitted = wired
    _answer(monkeypatch, [_candidate()])

    run = outreach_discovery.run_discovery(core, limit=3, dry_run=True)

    assert run["accepted"] == 1
    assert run["queued"] == 0
    assert submitted == []
    assert run["candidates"][0]["handle"] == "westside-rowing"


def test_a_real_run_queues_each_candidate(wired, monkeypatch):
    core, _ledger, submitted = wired
    _answer(monkeypatch, [_candidate()])

    run = outreach_discovery.run_discovery(core, limit=3)

    assert run["queued"] == 1
    assert len(submitted) == 1
    assert submitted[0]["screening_confirmed"] is True
    assert submitted[0]["email_authorized"] is False


def test_the_models_work_is_rechecked_before_anything_is_created(wired, monkeypatch):
    """The brief already forbids all of this. That is not the same as it being
    true, and a bad handle is cheaper to reject here than in Shopify."""
    core, _ledger, submitted = wired
    _answer(monkeypatch, [
        _candidate("st. mary's"),                                  # unusable handle
        _candidate("no-email", contact_email="see contact form"),  # not an address
        _candidate("http-logo", logo_source_url="http://x.org/a.png"),  # not https
        _candidate("good-club"),
    ])

    run = outreach_discovery.run_discovery(core, limit=10)

    assert [row["handle"] for row in run["candidates"]] == ["good-club"]
    assert len(run["rejected"]) == 3
    assert len(submitted) == 1


def test_a_store_we_already_know_is_never_offered_twice(wired, monkeypatch):
    core, ledger, submitted = wired
    ledger["westside-rowing"] = {"handle": "westside-rowing", "status": "provisioned"}
    _answer(monkeypatch, [_candidate()])

    run = outreach_discovery.run_discovery(core, limit=3)

    assert run["accepted"] == 0
    assert submitted == []
    assert run["rejected"][0]["reason"] == "already known"


def test_duplicates_inside_one_batch_are_caught(wired, monkeypatch):
    core, _ledger, submitted = wired
    _answer(monkeypatch, [_candidate(), _candidate()])

    run = outreach_discovery.run_discovery(core, limit=5)

    assert run["accepted"] == 1
    assert len(submitted) == 1


def test_a_failed_run_is_recorded_rather_than_lost(wired, monkeypatch):
    core, ledger, _submitted = wired

    def boom(limit, avoid):
        raise RuntimeError("OpenAI returned HTTP 404: unknown model")

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", boom)

    with pytest.raises(RuntimeError):
        outreach_discovery.run_discovery(core, limit=3)

    runs = ledger[outreach_discovery.RUN_LEDGER_HANDLE]["runs"]
    assert "unknown model" in runs[-1]["error"]


def test_the_run_history_stays_bounded(wired, monkeypatch):
    core, ledger, _submitted = wired
    _answer(monkeypatch, [])
    for _ in range(outreach_discovery._MAX_RUNS_KEPT + 6):
        outreach_discovery.run_discovery(core, limit=1, dry_run=True)
    assert len(ledger[outreach_discovery.RUN_LEDGER_HANDLE]["runs"]) == outreach_discovery._MAX_RUNS_KEPT


def test_the_run_ledger_row_is_not_a_store(wired, monkeypatch):
    """It shares the ledger with real stores, so it must never look like one to
    the review queue or the retention sweep."""
    import outreach_review

    core, ledger, _submitted = wired
    _answer(monkeypatch, [])
    outreach_discovery.run_discovery(core, limit=1, dry_run=True)

    monkeypatch.setattr(outreach_review.outreach_tracking, "list_all", lambda _c: dict(ledger))
    assert outreach_review.pending_queue(core) == []


def test_the_brief_tells_the_model_what_not_to_return(wired, monkeypatch):
    core, ledger, _submitted = wired
    ledger["already-built"] = {"handle": "already-built"}
    seen = {}

    def ask(limit, avoid):
        seen["avoid"] = avoid
        return [], {}

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", ask)
    outreach_discovery.run_discovery(core, limit=2, dry_run=True)
    assert "already-built" in seen["avoid"]
