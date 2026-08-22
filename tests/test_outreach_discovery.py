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
    def ask(limit, avoid, **_kwargs):
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

    def boom(limit, avoid, **_kwargs):
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

    def ask(limit, avoid, **kwargs):
        seen["avoid"] = avoid
        seen.update(kwargs)
        return [], {}

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", ask)
    outreach_discovery.run_discovery(core, limit=2, dry_run=True)
    assert "already-built" in seen["avoid"]


def test_the_same_organization_under_a_new_handle_is_caught(wired, monkeypatch):
    """A handle is a weak identity for an organization.

    The same fire department is cassie-vfd one night and
    cassie-volunteer-fire-department the next, and both pass a handle check.
    The website does not move.
    """
    core, ledger, submitted = wired
    ledger["westside-rowing"] = {
        "handle": "westside-rowing",
        "organization_url": "https://www.westside.org/about",
    }
    _answer(monkeypatch, [_candidate("westside-rowing-club")])

    run = outreach_discovery.run_discovery(core, limit=3)

    assert run["accepted"] == 0
    assert submitted == []
    assert "westside.org" in run["rejected"][0]["reason"]


def test_each_run_chases_a_different_slice_of_the_rotation(wired, monkeypatch):
    """Two runs in a row of nothing but fire departments is the failure this
    prevents. Variety comes from the schedule, not from hoping."""
    core, _ledger, _submitted = wired
    seen = []

    def ask(limit, avoid, **kwargs):
        seen.append(list(kwargs.get("focus") or []))
        return [], {}

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", ask)
    for _ in range(3):
        outreach_discovery.run_discovery(core, limit=2, dry_run=True)

    assert all(len(focus) == outreach_discovery._CATEGORIES_PER_RUN for focus in seen)
    # No category repeats while the rotation still has unused entries.
    flat = [item for focus in seen for item in focus]
    assert len(set(flat)) == len(flat)


def test_the_rotation_wraps_instead_of_running_out(wired, monkeypatch):
    core, _ledger, _submitted = wired
    seen = []

    def ask(limit, avoid, **kwargs):
        seen.append(list(kwargs.get("focus") or []))
        return [], {}

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", ask)
    for _ in range(len(outreach_discovery.CATEGORY_ROTATION) + 4):
        outreach_discovery.run_discovery(core, limit=1, dry_run=True)

    assert all(len(focus) == outreach_discovery._CATEGORIES_PER_RUN for focus in seen)
    assert seen[-1]  # still producing a focus after wrapping


def test_recent_categories_are_fed_back_as_things_to_skip(wired, monkeypatch):
    core, _ledger, _submitted = wired
    _answer(monkeypatch, [_candidate()])
    outreach_discovery.run_discovery(core, limit=2, dry_run=True)

    seen = {}

    def ask(limit, avoid, **kwargs):
        seen.update(kwargs)
        return [], {}

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", ask)
    outreach_discovery.run_discovery(core, limit=2, dry_run=True)

    assert "rowing club" in (seen.get("avoid_categories") or [])


def test_a_run_can_override_the_model_without_a_redeploy(wired, monkeypatch):
    """Comparing a cheap model against a better one should not require editing a
    Railway variable and waiting for a deploy between each comparison."""
    core, _ledger, _submitted = wired
    monkeypatch.setenv("OUTREACH_DISCOVERY_MODEL", "configured-default")
    seen = {}

    def ask(limit, avoid, **kwargs):
        seen.update(kwargs)
        return [], {"model": kwargs.get("model") or "configured-default"}

    monkeypatch.setattr(outreach_discovery, "_ask_for_candidates", ask)

    outreach_discovery.run_discovery(core, limit=1, dry_run=True, model="better-model")
    assert seen["model"] == "better-model"

    outreach_discovery.run_discovery(core, limit=1, dry_run=True)
    assert seen["model"] == ""  # falls back to the configured default


class FakeResponse:
    def __init__(self, status_code, text="", payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


_OK_BODY = {
    "output": [{"content": [{"type": "output_text", "text": json.dumps({"candidates": []})}]}],
    "usage": {"input_tokens": 10, "output_tokens": 2},
}

_RATE_LIMITED = (
    '{"error": {"message": "Rate limit reached for gpt-5.6-luna on tokens per min '
    '(TPM): Limit 200000, Used 185927, Requested 37853. Please try again in 7.134s.", '
    '"code": "rate_limit_exceeded"}}'
)


def test_a_rate_limit_waits_and_retries_instead_of_failing(monkeypatch):
    """A tokens-per-minute ceiling is a budget, not a fault. The response even
    says how long to wait, and the search has already been paid for."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []
    slept = []

    def post(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return FakeResponse(429, _RATE_LIMITED)
        return FakeResponse(200, payload=_OK_BODY)

    monkeypatch.setattr(outreach_discovery.requests, "post", post)
    monkeypatch.setattr(outreach_discovery.time, "sleep", lambda s: slept.append(s))

    candidates, telemetry = outreach_discovery._ask_for_candidates(2, [])

    assert candidates == []
    assert telemetry["attempts"] == 2
    # It waits the time the provider asked for, not a guess.
    assert 8.0 <= slept[0] <= 9.0


def test_a_bad_model_fails_immediately_without_retrying(monkeypatch):
    """A rename does not fix itself. Retrying it just spends the wait twice and
    delays the message that says what to change."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        return FakeResponse(404, '{"error":{"message":"unknown model"}}')

    monkeypatch.setattr(outreach_discovery.requests, "post", post)
    monkeypatch.setattr(outreach_discovery.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="unknown model"):
        outreach_discovery._ask_for_candidates(2, [])
    assert len(calls) == 1


def test_retrying_gives_up_and_reports_the_providers_message(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        return FakeResponse(429, _RATE_LIMITED)

    monkeypatch.setattr(outreach_discovery.requests, "post", post)
    monkeypatch.setattr(outreach_discovery.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="Rate limit reached"):
        outreach_discovery._ask_for_candidates(2, [])
    assert len(calls) == outreach_discovery._MAX_ATTEMPTS


def test_a_retry_never_waits_longer_than_the_cap(monkeypatch):
    """A browser is holding this request open, so the wait has to stay bounded
    even when the provider asks for minutes."""
    response = FakeResponse(429, "", headers={"retry-after": "600"})
    assert outreach_discovery._retry_after(response, 0) == outreach_discovery._MAX_RETRY_WAIT


def test_the_run_route_never_blocks_the_event_loop():
    """A run is minutes of blocking HTTP, and this process also serves the
    storefront. Calling it inline from an async route held the event loop for
    the whole run, so fundraiser lookups, store status and the review queue all
    queued behind it and the service looked down rather than busy."""
    import ast
    import inspect

    source = inspect.getsource(outreach_discovery.install_outreach_discovery_routes)
    tree = ast.parse(source.strip())

    run_route = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "discovery_run":
            run_route = node
    assert run_route is not None, "discovery_run is expected to be an async route"

    calls = [
        node for node in ast.walk(run_route)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "run_discovery"
    ]
    assert not calls, "run_discovery must not be called directly from the async route"

    threadpooled = [
        node for node in ast.walk(run_route)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "run_in_threadpool"
    ]
    assert threadpooled, "the run must go through run_in_threadpool"


def test_the_candidates_you_just_reviewed_are_the_ones_that_get_built(wired, monkeypatch):
    """Searching again to build them would cost another search and could return
    a different set. The reviewed ones should be the ones that get made."""
    core, _ledger, submitted = wired
    _answer(monkeypatch, [_candidate("first-club"), _candidate("second-club",
                                                               organization_url="https://second.org/",
                                                               contact_source_url="https://second.org/c",
                                                               logo_source_url="https://second.org/l.png")])

    test_run = outreach_discovery.run_discovery(core, limit=5, dry_run=True)
    assert test_run["queued"] == 0
    assert submitted == []

    built = outreach_discovery.build_last_run(core)

    assert built["queued"] == 2
    assert {row["storefront_handle"] for row in submitted} == {"first-club", "second-club"}
    assert built["trigger"] == "build-reviewed"


def test_building_twice_does_not_create_the_store_twice(wired, monkeypatch):
    core, ledger, submitted = wired
    _answer(monkeypatch, [_candidate("first-club")])
    outreach_discovery.run_discovery(core, limit=2, dry_run=True)
    outreach_discovery.build_last_run(core)
    assert len(submitted) == 1

    # The build itself is now the most recent run, so there is nothing pending.
    with pytest.raises(LookupError):
        outreach_discovery.build_last_run(core)
    assert len(submitted) == 1


def test_a_store_created_since_the_test_run_is_skipped(wired, monkeypatch):
    """Minutes or hours can pass between reading the candidates and building
    them, so the duplicate check is redone rather than trusted."""
    core, ledger, submitted = wired
    _answer(monkeypatch, [_candidate("first-club")])
    outreach_discovery.run_discovery(core, limit=2, dry_run=True)

    ledger["first-club"] = {"handle": "first-club", "status": "provisioned"}
    built = outreach_discovery.build_last_run(core)

    assert submitted == []
    assert built["rejected"][0]["reason"] == "already known"


def test_building_refuses_when_the_last_run_already_built(wired, monkeypatch):
    core, _ledger, _submitted = wired
    _answer(monkeypatch, [_candidate("first-club")])
    outreach_discovery.run_discovery(core, limit=2)  # a real run, not a test
    with pytest.raises(LookupError, match="not a test run"):
        outreach_discovery.build_last_run(core)
