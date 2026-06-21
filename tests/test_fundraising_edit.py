"""
Tests for the active-fundraiser EDIT flow (merge-update of goal / end_date /
show_bar). An edit must NEVER disable the fundraiser, change amount/cause/owner/
Stripe/raised totals, or reprice products.

Mirrors the mocking style of test_fundraising_hardening.py — no network calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import app as studio_app
from fastapi.testclient import TestClient


ADMIN_SECRET = "test-admin-secret-abc123"
SUPERADMIN_ID = "99999"
OWNER_ID = "12345"
OTHER_ID = "67890"


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


def _fr_state_gql(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "metaobjectByHandle": {
            "id": "gid://shopify/Metaobject/42",
            "fields": [{"key": "data", "value": json.dumps(state)}],
        }
    }


def _definition_gql() -> Dict[str, Any]:
    return {"metaobjectDefinitionByType": {"id": "gid://shopify/MetaobjectDefinition/1"}}


def _upsert_ok() -> Dict[str, Any]:
    return {"metaobjectUpsert": {"metaobject": {"id": "gid://shopify/Metaobject/42", "handle": "test-store"}, "userErrors": []}}


def _admin_headers(customer_id: str | None = None) -> dict:
    h = {"X-Admin-Secret": ADMIN_SECRET}
    if customer_id:
        h["X-SS-Customer-Id"] = customer_id
    return h


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "shopify-access-token")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("FUNDRAISING_SUPERADMIN_CUSTOMER_IDS", SUPERADMIN_ID)
    monkeypatch.setattr(studio_app, "_ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setattr(studio_app, "_FR_SUPERADMIN_IDS", frozenset([SUPERADMIN_ID]))
    monkeypatch.setattr(studio_app, "_SHOPIFY_SHOP", "example.myshopify.com")
    monkeypatch.setattr(studio_app, "_SHOPIFY_ACCESS_TOKEN", "shopify-access-token")


def _wire(monkeypatch, existing_state: Dict[str, Any], saved_state: Dict[str, Any], threads: List):
    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        if "metaobjectDefinitionByType" in q:
            return _gql_resp(_definition_gql())
        if "metaobjectByHandle" in q:
            return _gql_resp(_fr_state_gql(existing_state))
        if "metaobjectUpsert" in q:
            fields = (json.get("variables", {}).get("metaobject", {}).get("fields") or [])
            for f in fields:
                if f.get("key") == "data":
                    saved_state.clear()
                    saved_state.update(studio_app.json.loads(f["value"]))
            return _gql_resp(_upsert_ok())
        return _gql_resp({})

    class _FakeThread:
        def __init__(self, *a, **kw): pass
        def start(self): threads.append(True)

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", _FakeThread)


# ---------------------------------------------------------------------------

def test_edit_merges_only_allowed_fields_and_preserves_everything(monkeypatch):
    existing = {
        "enabled": True, "amount": 5, "cause_name": "Santee Soccer Club",
        "goal": 500, "end_date": "2026-06-29", "show_bar": True,
        "owner_customer_id": OWNER_ID, "owner_email": "coach@example.com",
        "stripe_account_id": "acct_real", "stripe_connected": True,
        "total_raised": 123.45, "markup_add": 6, "created_at": "2026-01-01T00:00:00+00:00",
        "ledger": [{"order_id": "1", "amount": 5, "paid": False}],
    }
    saved: dict = {}
    threads: list = []
    _wire(monkeypatch, existing, saved, threads)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={"action": "update", "mode": "edit", "goal": 750,
              "end_date": "2026-07-15", "show_bar": False},
    )
    assert res.status_code == 200, res.text
    # Only the three editable fields changed.
    assert saved["goal"] == 750
    assert saved["end_date"] == "2026-07-15"
    assert saved["show_bar"] is False
    # Everything else preserved.
    assert saved["enabled"] is True
    assert saved["amount"] == 5
    assert saved["cause_name"] == "Santee Soccer Club"
    assert saved["owner_customer_id"] == OWNER_ID
    assert saved["stripe_account_id"] == "acct_real"
    assert saved["stripe_connected"] is True
    assert saved["total_raised"] == 123.45
    assert saved["markup_add"] == 6
    assert saved["created_at"] == "2026-01-01T00:00:00+00:00"
    assert saved["ledger"] == [{"order_id": "1", "amount": 5, "paid": False}]
    # No repricing thread started on an edit.
    assert threads == []
    assert saved.get("pricing_status") == "skipped"
    # Response is normalized.
    body = res.json()
    assert body["ok"] is True and body["enabled"] is True and body["active"] is True
    assert body["goal"] == 750 and body["show_bar"] is False


def test_edit_without_enabled_field_does_not_disable(monkeypatch):
    """The original corruption: an edit payload with no `enabled` must NOT stop it."""
    existing = {"enabled": True, "amount": 4, "cause_name": "Team",
                "goal": 300, "owner_customer_id": OWNER_ID, "markup_add": 5,
                "total_raised": 10}
    saved: dict = {}
    threads: list = []
    _wire(monkeypatch, existing, saved, threads)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={"action": "update", "goal": 999},  # no enabled key at all
    )
    assert res.status_code == 200, res.text
    assert saved["enabled"] is True          # still active
    assert saved["amount"] == 4              # unchanged
    assert saved["goal"] == 999
    assert threads == []                     # no reprice


def test_edit_on_stopped_fundraiser_returns_409(monkeypatch):
    existing = {"enabled": False, "amount": 4, "cause_name": "Team",
                "owner_customer_id": OWNER_ID}
    saved: dict = {}
    threads: list = []
    _wire(monkeypatch, existing, saved, threads)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={"action": "update", "goal": 500},
    )
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["ok"] is False
    assert body.get("code") == "no_active_fundraiser"
    assert saved == {}  # nothing written


def test_edit_by_non_owner_returns_403(monkeypatch):
    existing = {"enabled": True, "amount": 4, "owner_customer_id": OWNER_ID}
    saved: dict = {}
    threads: list = []
    _wire(monkeypatch, existing, saved, threads)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OTHER_ID),
        json={"action": "update", "goal": 500},
    )
    assert res.status_code == 403, res.text
    assert saved == {}


def test_edit_accepts_field_aliases(monkeypatch):
    """Legacy field names (fundraising_goal, visibility, fundraiser_end_date) map in."""
    existing = {"enabled": True, "amount": 4, "goal": 100, "show_bar": True,
                "owner_customer_id": OWNER_ID}
    saved: dict = {}
    threads: list = []
    _wire(monkeypatch, existing, saved, threads)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/test-store",
        headers=_admin_headers(customer_id=OWNER_ID),
        json={"mode": "edit", "fundraising_goal": 850,
              "fundraiser_end_date": "2026-08-01", "visibility": "admin_only"},
    )
    assert res.status_code == 200, res.text
    assert saved["goal"] == 850
    assert saved["end_date"] == "2026-08-01"
    assert saved["show_bar"] is False


def test_get_returns_normalized_active_and_days_left(monkeypatch):
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=9)).date().isoformat()
    existing = {"enabled": True, "amount": 4, "cause_name": "Team",
                "goal": 500, "end_date": future, "show_bar": True,
                "total_raised": 40, "owner_customer_id": OWNER_ID}
    saved: dict = {}
    threads: list = []
    _wire(monkeypatch, existing, saved, threads)

    client = TestClient(studio_app.app)
    res = client.get("/api/fundraising/test-store", headers=_admin_headers(customer_id=OWNER_ID))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["enabled"] is True
    assert body["active"] is True
    assert body["goal"] == 500.0
    assert body["amount"] == 4
    assert body["end_date"] == future
    assert body["show_bar"] is True
    assert body["total_raised"] == 40.0
    assert body["days_left"] in (8, 9)  # depending on time-of-day rounding
