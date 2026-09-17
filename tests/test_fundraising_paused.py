"""The pause blocks setup before any remote call, but preserves safe shutdown."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as studio_app


@pytest.fixture
def paused(monkeypatch):
    monkeypatch.delenv("FUNDRAISING_SETUP_ENABLED", raising=False)
    monkeypatch.setattr(studio_app, "_ADMIN_SECRET", "test-pause-secret")
    monkeypatch.setenv("ADMIN_SECRET", "test-pause-secret")
    def unexpected(*args, **kwargs):
        raise AssertionError("Paused setup must not call Shopify or Stripe")
    monkeypatch.setattr(studio_app.requests, "post", unexpected)
    return TestClient(studio_app.app)


HEADERS = {"X-Admin-Secret": "test-pause-secret", "X-SS-Customer-Id": "12345"}


@pytest.mark.parametrize("body", [
    {}, {"enabled": True}, {"amount": 5}, {"action": "update", "goal": 100},
    {"enabled": False, "action": "update", "goal": 100},
])
def test_setup_is_paused_by_default(paused, body):
    response = paused.post("/api/fundraising/test-store", headers=HEADERS, json=body)
    assert response.status_code == 503
    assert response.json()["code"] == "fundraising_coming_soon"


def test_stripe_onboarding_is_paused(paused):
    response = paused.post("/api/fundraising/test-store/stripe/connect", headers=HEADERS, json={})
    assert response.status_code == 503
    assert response.json()["code"] == "fundraising_coming_soon"


def test_pause_does_not_bypass_authentication(paused):
    response = paused.post("/api/fundraising/test-store", json={"enabled": False})
    assert response.status_code in (401, 403)


def test_owner_can_stop_without_changing_existing_obligations(paused, monkeypatch):
    existing = {"enabled": True, "owner_customer_id": "12345", "amount": 4,
                "markup_add": 5, "total_raised": 60, "stripe_account_id": "acct_existing",
                "ledger": [{"order_id": "old", "amount": 4, "paid": False}]}
    saved = {}
    repricing = []
    monkeypatch.setattr(studio_app, "_ensure_fundraising_definition", lambda: None)
    monkeypatch.setattr(studio_app, "_fr_get_state", lambda h: existing)
    monkeypatch.setattr(studio_app, "_fr_set_state", lambda h, s: saved.update(s))
    class Thread:
        def __init__(self, target, args, daemon):
            self.args = args
        def start(self):
            repricing.append(self.args)
    monkeypatch.setattr(studio_app.threading, "Thread", Thread)
    response = paused.post("/api/fundraising/test-store", headers=HEADERS,
                           json={"enabled": False, "amount": 8, "cause_name": "Changed"})
    assert response.status_code == 200, response.text
    assert saved["enabled"] is False
    for key in ("amount", "total_raised", "stripe_account_id", "ledger", "owner_customer_id"):
        assert saved[key] == existing[key]
    assert "cause_name" not in saved
    assert repricing == [("test-store",)]
