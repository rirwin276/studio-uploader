from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import prospect_demo


class FakeCore:
    """``shopify_owner`` is what Shopify would report for the store's owner —
    empty while unclaimed, a customer id once somebody has claimed it."""

    def __init__(self, shopify_owner: str = ""):
        self.shopify_owner = shopify_owner

    def _require_admin_secret(self, request: Request):
        if request.headers.get("X-Admin-Secret") != "test-secret":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    def _fr_get_owner_from_custom_shop(self, _handle: str) -> str:
        return self.shopify_owner


def _client(monkeypatch, source="direct_outreach_api", shopify_owner=""):
    states = {
        "example-club": {
            "handle": "example-club",
            "source": source,
            "store_status": "prospect_unclaimed",
            "claim_status": "unclaimed",
        }
    }

    def read(_core, handle):
        return dict(states.get(handle, {}))

    def upsert(_core, handle, state):
        states[handle] = dict(state)

    monkeypatch.setattr(prospect_demo.outreach_tracking, "read", read)
    monkeypatch.setattr(prospect_demo.outreach_tracking, "upsert", upsert)
    app = FastAPI()
    prospect_demo.install_prospect_demo_routes(app, FakeCore(shopify_owner))
    return TestClient(app), states


def _headers():
    return {"X-Admin-Secret": "test-secret"}


def test_demo_state_requires_private_relay_secret(monkeypatch):
    client, _states = _client(monkeypatch)
    assert client.get("/api/outreach/store/example-club/demo-state").status_code == 401


def test_one_real_demo_product_is_reserved_and_completed(monkeypatch):
    client, states = _client(monkeypatch)
    reserve = client.post(
        "/api/outreach/store/example-club/demo-product/reserve",
        headers=_headers(),
        json={"model": "bc3413", "request_id": "session-1", "job_id": "job-1"},
    )
    assert reserve.status_code == 200
    reservation_id = reserve.json()["reservation_id"]
    assert states["example-club"]["prospect_demo"]["product_status"] == "reserved"

    blocked = client.post(
        "/api/outreach/store/example-club/demo-product/reserve",
        headers=_headers(),
        json={"model": "m2580", "request_id": "session-2", "job_id": "job-2"},
    )
    assert blocked.status_code == 409
    assert "Claim your free store" in blocked.json()["error"]

    complete = client.post(
        "/api/outreach/store/example-club/demo-product/complete",
        headers=_headers(),
        json={
            "reservation_id": reservation_id,
            "product_id": "gid://shopify/Product/123",
            "product_handle": "example-club-triblend",
        },
    )
    assert complete.status_code == 200
    demo = states["example-club"]["prospect_demo"]
    assert demo["product_status"] == "completed"
    assert demo["product_id"] == "gid://shopify/Product/123"
    assert demo["event_counts"]["demo_product_successfully_created"] == 1

    still_blocked = client.post(
        "/api/outreach/store/example-club/demo-product/reserve",
        headers=_headers(),
        json={"model": "m2580", "request_id": "session-3", "job_id": "job-3"},
    )
    assert still_blocked.status_code == 409


def test_failed_build_releases_allowance_for_retry(monkeypatch):
    client, states = _client(monkeypatch)
    reserve = client.post(
        "/api/outreach/store/example-club/demo-product/reserve",
        headers=_headers(),
        json={"model": "bc3413", "request_id": "session-1", "job_id": "job-1"},
    )
    reservation_id = reserve.json()["reservation_id"]
    failed = client.post(
        "/api/outreach/store/example-club/demo-product/fail",
        headers=_headers(),
        json={"reservation_id": reservation_id, "error": "Printful unavailable"},
    )
    assert failed.status_code == 200
    assert states["example-club"]["prospect_demo"]["product_status"] == "available"

    retry = client.post(
        "/api/outreach/store/example-club/demo-product/reserve",
        headers=_headers(),
        json={"model": "bc3413", "request_id": "session-2", "job_id": "job-2"},
    )
    assert retry.status_code == 200


def test_claim_disables_demo_and_records_conversion(monkeypatch):
    client, states = _client(monkeypatch)
    prospect_demo.mark_claimed(FakeCore(), "example-club", "101")

    state = client.get(
        "/api/outreach/store/example-club/demo-state",
        headers=_headers(),
    )
    assert state.status_code == 200
    assert state.json()["enabled"] is False
    assert state.json()["claim_status"] == "claimed"
    assert states["example-club"]["prospect_demo"]["event_counts"]["store_successfully_claimed"] == 1
    prospect_demo.mark_claimed(FakeCore(), "example-club", "101")
    assert states["example-club"]["prospect_demo"]["event_counts"]["store_successfully_claimed"] == 1


def test_events_are_allowlisted(monkeypatch):
    client, states = _client(monkeypatch)
    response = client.post(
        "/api/outreach/store/example-club/demo-event",
        headers=_headers(),
        json={"event": "admin_demo_opened", "session_id": "browser-1"},
    )
    assert response.status_code == 200
    assert states["example-club"]["prospect_demo"]["event_counts"]["admin_demo_opened"] == 1

    rejected = client.post(
        "/api/outreach/store/example-club/demo-event",
        headers=_headers(),
        json={"event": "delete_everything"},
    )
    assert rejected.status_code == 400


def test_json_intake_stores_get_the_same_demo_as_multipart_stores(monkeypatch):
    """The JSON intake queue writes its own ``source`` value.

    Every store the nightly pipeline builds arrives with
    ``vendor_neutral_outreach_intake`` instead of ``direct_outreach_api``.  A
    single-string comparison silently disabled the demo, the appearance editor
    and claim tracking for exactly those stores, so pin the shared behavior.
    """
    client, states = _client(monkeypatch, source="vendor_neutral_outreach_intake")

    state = client.get("/api/outreach/store/example-club/demo-state", headers=_headers())
    assert state.status_code == 200
    assert state.json()["enabled"] is True

    event = client.post(
        "/api/outreach/store/example-club/demo-event",
        headers=_headers(),
        json={"event": "store_appearance_changed", "session_id": "browser-1"},
    )
    assert event.status_code == 200
    assert states["example-club"]["prospect_demo"]["event_counts"]["store_appearance_changed"] == 1

    prospect_demo.mark_claimed(FakeCore(), "example-club", "101")
    assert states["example-club"]["claim_status"] == "claimed"
    assert states["example-club"]["claimed_customer_id"] == "101"


def test_a_website_store_is_never_treated_as_a_prospect(monkeypatch):
    client, _states = _client(monkeypatch, source="website_request_form")
    state = client.get("/api/outreach/store/example-club/demo-state", headers=_headers())
    assert state.status_code == 200
    assert state.json()["enabled"] is False


def test_anonymous_demo_uses_same_one_product_controls_but_separate_state(monkeypatch):
    client, states = _client(monkeypatch, source="anonymous_demo")
    states["example-club"]["store_status"] = "anonymous_demo_unclaimed"

    state = client.get("/api/outreach/store/example-club/demo-state", headers=_headers())
    assert state.status_code == 200
    assert state.json()["enabled"] is True

    prospect_demo.mark_claimed(FakeCore(), "example-club", "101")
    assert states["example-club"]["claim_status"] == "claimed"
    assert states["example-club"]["expires_at"] is None


def test_anonymous_source_with_outreach_state_is_rejected(monkeypatch):
    client, _states = _client(monkeypatch, source="anonymous_demo")
    state = client.get("/api/outreach/store/example-club/demo-state", headers=_headers())
    assert state.status_code == 200
    assert state.json()["enabled"] is False


def test_a_claimed_store_offers_no_demo_even_if_the_ledger_missed_the_claim(monkeypatch):
    """The join route grants the claim in Shopify, then calls mark_claimed inside
    a try/except so demo bookkeeping can never fail a claim that already
    succeeded. When that call is the thing that fails, claim_status stays
    "unclaimed" on a store that genuinely has an owner — and members the new
    admin invites would be handed the admin demo. Shopify is the authority."""
    client, states = _client(monkeypatch, shopify_owner="8899")
    assert states["example-club"]["claim_status"] == "unclaimed"

    state = client.get("/api/outreach/store/example-club/demo-state", headers=_headers())
    assert state.status_code == 200
    assert state.json()["enabled"] is False

    # ...and the stale ledger row repairs itself rather than being re-checked
    # against Shopify on every single page view.
    assert states["example-club"]["claim_status"] == "claimed"


def test_a_claimed_store_refuses_demo_events_and_products(monkeypatch):
    client, _states = _client(monkeypatch, shopify_owner="8899")

    event = client.post(
        "/api/outreach/store/example-club/demo-event",
        headers=_headers(),
        json={"event": "store_appearance_changed", "session_id": "browser-1"},
    )
    assert event.status_code == 409

    reserve = client.post(
        "/api/outreach/store/example-club/demo-product/reserve",
        headers=_headers(),
        json={"model": "bc3413", "request_id": "session-1", "job_id": "job-1"},
    )
    assert reserve.status_code == 409


def test_an_unclaimed_store_is_unaffected_by_the_owner_check(monkeypatch):
    client, states = _client(monkeypatch, shopify_owner="")
    state = client.get("/api/outreach/store/example-club/demo-state", headers=_headers())
    assert state.json()["enabled"] is True
    assert states["example-club"]["claim_status"] == "unclaimed"


# ---- the founder checking his own work is not traction --------------------


class _StaffRequest:
    def __init__(self, staff: bool):
        self.headers = {"X-SS-Staff": "1"} if staff else {}


def test_a_staff_event_is_kept_but_never_counted():
    """Checking your own work must not read as interest: the funnel would
    report traction nobody had, and retention would keep a store alive because
    its author was the one poking at it."""
    import prospect_demo

    state = {}
    prospect_demo._append_event(state, "admin_demo_opened", staff=True)
    demo = state["prospect_demo"]

    assert demo["event_counts"] == {}
    assert demo["events"][-1]["staff"] is True
    assert "last_admin_demo_opened_at" not in demo


def test_a_prospect_event_still_counts():
    import prospect_demo

    state = {}
    prospect_demo._append_event(state, "admin_demo_opened")
    demo = state["prospect_demo"]

    assert demo["event_counts"]["admin_demo_opened"] == 1
    assert "staff" not in demo["events"][-1]
    assert demo["last_admin_demo_opened_at"]


def test_the_staff_marker_only_comes_from_the_relay():
    import prospect_demo

    assert prospect_demo.is_staff_request(_StaffRequest(True)) is True
    assert prospect_demo.is_staff_request(_StaffRequest(False)) is False
    assert prospect_demo.is_staff_request(object()) is False
