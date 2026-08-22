"""
Tests for fundraising hardening (owner capture, owner enforcement,
one-at-a-time, Stripe field lockdown, nuke guard, webhook idempotency).

Pattern mirrors test_ready_status_handoff.py:
  - monkeypatch app.requests.post to return fake Shopify GraphQL responses
  - monkeypatch os.environ for secrets
  - TestClient for HTTP calls

No network calls are made.
"""
from __future__ import annotations

import json
import hashlib
import hmac
from pathlib import Path
from typing import Any, Dict, List
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import app as studio_app
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ADMIN_SECRET = "test-admin-secret-abc123"
SUPERADMIN_ID = "99999"
OWNER_ID = "12345"
OTHER_ID = "67890"
WEBHOOK_SECRET = "webhook-secret-xyz"


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _gql_resp(data: Dict[str, Any]) -> _FakeResponse:
    return _FakeResponse({"data": data})


def _make_fr_state_gql(state: Dict[str, Any]) -> Dict[str, Any]:
    """Produce the metaobjectByHandle GraphQL response for a fundraiser state."""
    return {
        "metaobjectByHandle": {
            "id": "gid://shopify/Metaobject/42",
            "fields": [{"key": "data", "value": json.dumps(state)}],
        }
    }


def _stripe_ready() -> Dict[str, Any]:
    """A not-yet-launched campaign that already has somewhere to send the money.

    Launching without a connected payout account is refused, so any test that
    exercises a first launch for some other reason has to start from here.
    """
    return {"stripe_connected": True, "stripe_account_id": "acct_test"}


def _make_definition_gql(exists: bool = True) -> Dict[str, Any]:
    if exists:
        return {"metaobjectDefinitionByType": {"id": "gid://shopify/MetaobjectDefinition/1"}}
    return {"metaobjectDefinitionByType": None}


def _upsert_ok() -> Dict[str, Any]:
    return {"metaobjectUpsert": {"metaobject": {"id": "gid://shopify/Metaobject/42", "handle": "test-store"}, "userErrors": []}}


def _admin_headers(customer_id: str | None = None, superadmin: str | None = None) -> dict:
    h = {"X-Admin-Secret": ADMIN_SECRET}
    if customer_id:
        h["X-SS-Customer-Id"] = customer_id
    if superadmin:
        h["X-SS-Superadmin"] = superadmin
    return h


def _shopify_webhook_hmac(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "shopify-access-token")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("FUNDRAISING_SUPERADMIN_CUSTOMER_IDS", SUPERADMIN_ID)
    # Reload module-level constants that read env at import time.
    monkeypatch.setattr(studio_app, "_ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setattr(studio_app, "_FR_SUPERADMIN_IDS", frozenset([SUPERADMIN_ID]))
    monkeypatch.setattr(studio_app, "_SHOPIFY_SHOP", "example.myshopify.com")
    monkeypatch.setattr(studio_app, "_SHOPIFY_ACCESS_TOKEN", "shopify-access-token")
    monkeypatch.setattr(studio_app, "_SHOPIFY_WEBHOOK_SECRET", WEBHOOK_SECRET)


# ---------------------------------------------------------------------------
# 1. Owner capture on launch
# ---------------------------------------------------------------------------

def test_launch_stamps_owner_from_header(monkeypatch):
    """First launch: owner_customer_id is taken from X-SS-Customer-Id header."""
    call_log: List[dict] = []
    saved_state: dict = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            # No campaign yet, but Stripe is connected — launching without a
            # payout account is refused, and this test is about owner stamping.
            return _gql_resp(_make_fr_state_gql(_stripe_ready()))
        if "metaobjectUpsert" in q:
            # Capture the state that was saved.
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        if "customer(id:" in q:
            return _gql_resp({"customer": {"email": "owner@example.com"}})
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    # Disable the background pricing thread so it doesn't try to call Shopify.
    monkeypatch.setattr(studio_app.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={"enabled": True, "cause_name": "Baseball", "amount": 5, "goal": 500},
    )
    assert res.status_code == 200, res.text
    assert saved_state.get("owner_customer_id") == OWNER_ID
    assert saved_state.get("owner_email") == "owner@example.com"


def test_launch_falls_back_to_custom_shop_owner(monkeypatch):
    """First launch without X-SS-Customer-Id: owner falls back to custom_shop metaobject."""
    saved_state: dict = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        variables = (json or {}).get("variables", {})
        handle_input = variables.get("handle", {})

        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            # Distinguish between store_fundraising and custom_shop reads.
            mo_type = handle_input.get("type", "")
            if mo_type == "store_fundraising":
                return _gql_resp(_make_fr_state_gql(_stripe_ready()))
            if mo_type in ("custom_shop", ""):
                # custom_shop with owner_customer_id
                return _gql_resp({
                    "metaobjectByHandle": {
                        "id": "gid://shopify/Metaobject/10",
                        "fields": [
                            {"key": "owner_customer_id", "value": OWNER_ID},
                        ],
                    }
                })
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        if "customer(id:" in q:
            return _gql_resp({"customer": {"email": "fallback@example.com"}})
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())

    client = TestClient(studio_app.app)
    # No X-SS-Customer-Id header.
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(),
        json={"enabled": True, "cause_name": "Baseball", "amount": 5, "goal": 500},
    )
    assert res.status_code == 200, res.text
    assert saved_state.get("owner_customer_id") == OWNER_ID


# ---------------------------------------------------------------------------
# 2. One fundraiser at a time
# ---------------------------------------------------------------------------

def test_second_launch_returns_409(monkeypatch):
    """A launch when a fundraiser is already active returns 409."""
    existing_state = {"enabled": True, "cause_name": "Old cause", "owner_customer_id": OWNER_ID}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)

    client = TestClient(studio_app.app)
    # A *different* customer trying to start a second fundraiser.
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OTHER_ID),
        json={"enabled": True, "cause_name": "New cause", "amount": 5},
    )
    assert res.status_code == 409, res.text
    assert "already running" in res.json().get("error", "").lower()


def test_owner_can_edit_active_fundraiser(monkeypatch):
    """The owner can POST to an active fundraiser (edit, not a second launch)."""
    existing_state = {"enabled": True, "cause_name": "Old cause", "owner_customer_id": OWNER_ID}
    saved_state: dict = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={"enabled": True, "cause_name": "Updated cause", "amount": 7},
    )
    assert res.status_code == 200, res.text
    assert saved_state.get("cause_name") == "Updated cause"


# ---------------------------------------------------------------------------
# 3. Owner enforcement — STOP and Stripe routes
# ---------------------------------------------------------------------------

def test_non_owner_stop_returns_403(monkeypatch):
    """A non-owner cannot stop the fundraiser."""
    existing_state = {"enabled": True, "cause_name": "Baseball", "owner_customer_id": OWNER_ID}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OTHER_ID),
        json={"enabled": False},
    )
    assert res.status_code == 403, res.text
    assert "organizer" in res.json().get("error", "").lower()


def test_owner_can_stop_fundraiser(monkeypatch):
    """The owner can stop their fundraiser."""
    existing_state = {"enabled": True, "cause_name": "Baseball", "owner_customer_id": OWNER_ID}
    saved_state: dict = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={"enabled": False},
    )
    assert res.status_code == 200, res.text
    assert saved_state.get("enabled") is False


def test_superadmin_can_stop_any_fundraiser(monkeypatch):
    """A super-admin can stop any fundraiser (super-admin override via header)."""
    existing_state = {"enabled": True, "cause_name": "Baseball", "owner_customer_id": OWNER_ID}
    saved_state: dict = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())

    client = TestClient(studio_app.app)
    # Super-admin override via X-SS-Superadmin: 1 (no customer id needed).
    res = client.post(
        "/api/fundraising/test-store",
        headers={**_admin_headers(), "X-SS-Superadmin": "1"},
        json={"enabled": False},
    )
    assert res.status_code == 200, res.text
    assert saved_state.get("enabled") is False


def test_superadmin_by_id_can_stop_any_fundraiser(monkeypatch):
    """A super-admin identified by customer id can stop any fundraiser."""
    existing_state = {"enabled": True, "cause_name": "Baseball", "owner_customer_id": OWNER_ID}
    saved_state: dict = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())

    client = TestClient(studio_app.app)
    # SUPERADMIN_ID is in FUNDRAISING_SUPERADMIN_CUSTOMER_IDS.
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=SUPERADMIN_ID),
        json={"enabled": False},
    )
    assert res.status_code == 200, res.text
    assert saved_state.get("enabled") is False


def test_non_owner_stripe_connect_returns_403(monkeypatch):
    """A non-owner cannot trigger Stripe connect once an owner is set."""
    existing_state = {"enabled": True, "owner_customer_id": OWNER_ID}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store/stripe/connect",
        headers=_admin_headers(customer_id=OTHER_ID),
        json={"return_url": "https://example.com/return"},
    )
    # 200 with ok:false (App Proxy pattern) or 403.
    data = res.json()
    assert res.status_code in (200, 403), res.text
    assert data.get("ok") is False or res.status_code == 403


# ---------------------------------------------------------------------------
# 4. Stripe fields are NOT client-writable
# ---------------------------------------------------------------------------

def test_stripe_fields_ignored_in_save_body(monkeypatch):
    """stripe_account_id and stripe_connected in the POST body must be ignored."""
    existing_state = {
        "enabled": True,
        "cause_name": "Old",
        "stripe_account_id": "acct_real",
        "stripe_connected": True,
        "owner_customer_id": OWNER_ID,
    }
    saved_state: dict = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_make_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(existing_state))
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())

    client = TestClient(studio_app.app)
    # Attacker tries to overwrite Stripe fields via the save route.
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={
            "enabled": True,
            "cause_name": "New name",
            "stripe_account_id": "acct_fake",
            "stripe_connected": False,
        },
    )
    assert res.status_code == 200, res.text
    # Server state must still have the original Stripe values.
    assert saved_state.get("stripe_account_id") == "acct_real"
    assert saved_state.get("stripe_connected") is True


# ---------------------------------------------------------------------------
# 5. Nuke guard
# ---------------------------------------------------------------------------

def test_nuke_blocked_active_fundraiser(monkeypatch):
    """Nuke returns 409 when fundraiser is enabled."""
    fr_state = {"enabled": True, "cause_name": "Baseball", "owner_customer_id": OWNER_ID}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(fr_state))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/storefront/test-store/nuke",
        headers=_admin_headers(),
        json={},
    )
    assert res.status_code == 409, res.text
    assert "active fundraiser" in res.json().get("error", "").lower()


def test_nuke_blocked_unpaid_ledger(monkeypatch):
    """Nuke returns 409 when there are unpaid ledger rows."""
    fr_state = {
        "enabled": False,
        "ledger": [
            {"order_id": "123", "amount": 5, "paid": False},
        ],
    }

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(fr_state))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/storefront/test-store/nuke",
        headers=_admin_headers(),
        json={},
    )
    assert res.status_code == 409, res.text
    assert "unpaid" in res.json().get("error", "").lower()


def test_nuke_allowed_clean_state(monkeypatch):
    """Nuke proceeds when fundraiser is not active and no unpaid ledger rows."""
    fr_state = {
        "enabled": False,
        "ledger": [
            {"order_id": "123", "amount": 5, "paid": True},  # all paid
        ],
    }

    job_started: list = []

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(fr_state))
        return _gql_resp({})

    class _FakeThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            job_started.append(True)

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", _FakeThread)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/storefront/test-store/nuke",
        headers=_admin_headers(),
        json={},
    )
    assert res.status_code == 200, res.text
    assert "job_id" in res.json()
    assert job_started, "Thread.start() was not called"


def test_nuke_superadmin_force_active_fundraiser(monkeypatch):
    """Super-admin can force-nuke even with an active fundraiser."""
    fr_state = {"enabled": True, "owner_customer_id": OWNER_ID}

    job_started: list = []

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(fr_state))
        return _gql_resp({})

    class _FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            job_started.append(True)

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", _FakeThread)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/storefront/test-store/nuke",
        headers={**_admin_headers(customer_id=SUPERADMIN_ID), "X-SS-Superadmin": "1"},
        json={"force": True},
    )
    assert res.status_code == 200, res.text
    assert "job_id" in res.json()
    assert job_started


# ---------------------------------------------------------------------------
# 6. Webhook idempotency
# ---------------------------------------------------------------------------

def _make_webhook_body(order: dict) -> bytes:
    return json.dumps(order).encode()


def _webhook_headers(body: bytes) -> dict:
    import base64
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()
    sig = base64.b64encode(digest).decode()
    return {"X-Shopify-Hmac-Sha256": sig}


def test_webhook_empty_order_id_uses_admin_graphql_api_id(monkeypatch):
    """When order id is empty, fallback to admin_graphql_api_id for deduplication."""
    # Start with a clean ledger.
    # A contribution is only credited when the line actually paid base + markup,
    # so the campaign has to carry the price snapshot it was launched with.
    fr_state = {
        "enabled": True,
        "amount": 5,
        "ledger": [],
        "total_raised": 0,
        "markup_add": 5,
        "base_prices": {"gid://shopify/ProductVariant/222": "20.00"},
    }
    saved_states: list = []

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        variables = (json or {}).get("variables", {})
        if "metaobjectByHandle" in q:
            handle_type = variables.get("handle", {}).get("type", "")
            if handle_type == "store_fundraising":
                return _gql_resp(_make_fr_state_gql(fr_state))
            return _gql_resp({"metaobjectByHandle": None})
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    new_state = studio_app.json.loads(f["value"])
                    saved_states.append(new_state)
                    fr_state.update(new_state)
            return _gql_resp(_upsert_ok())
        # _product_tags: query for a single product by ID
        if "product(id:" in q:
            return _gql_resp({
                "product": {"tags": ["test-store"]},
            })
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(studio_app, "_SHOPIFY_WEBHOOK_SECRET", WEBHOOK_SECRET)

    order = {
        "id": "",  # empty — the bug path
        "admin_graphql_api_id": "gid://shopify/Order/9001",
        "line_items": [
            {"product_id": "111", "variant_id": "222", "price": "25.00", "quantity": 1},
        ],
    }
    body = _make_webhook_body(order)
    headers = _webhook_headers(body)

    client = TestClient(studio_app.app)

    # Send the webhook twice — should only count once.
    res1 = client.post("/webhooks/fundraising/order-paid", content=body, headers=headers)
    assert res1.status_code == 200, res1.text

    res2 = client.post("/webhooks/fundraising/order-paid", content=body, headers=headers)
    assert res2.status_code == 200, res2.text

    # Only one ledger row should exist.
    ledger = fr_state.get("ledger", [])
    assert len(ledger) == 1, f"Expected 1 ledger row, got {len(ledger)}: {ledger}"
    assert ledger[0]["order_id"] == "gid://shopify/Order/9001"


def test_webhook_truly_no_order_id_is_skipped(monkeypatch):
    """When there is truly no order id at all, the webhook is skipped without appending a row."""
    fr_state = {"enabled": True, "amount": 5, "ledger": [], "total_raised": 0}

    def fake_post(url, headers=None, json=None, **kwargs):
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(studio_app, "_SHOPIFY_WEBHOOK_SECRET", WEBHOOK_SECRET)

    order = {
        # No id, no admin_graphql_api_id, no order_number
        "line_items": [{"product_id": "111", "quantity": 1}],
    }
    body = _make_webhook_body(order)
    headers = _webhook_headers(body)

    client = TestClient(studio_app.app)
    res = client.post("/webhooks/fundraising/order-paid", content=body, headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data.get("skipped") == "no_order_id"
    assert len(fr_state.get("ledger", [])) == 0
