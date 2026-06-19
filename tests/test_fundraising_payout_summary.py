"""
Tests for:
  Part A — GET /api/fundraising/payouts/summary (read-only super-admin endpoint)
  Part B — fundraising_payouts_run safety fixes (idempotency key, batch marker)

Pattern mirrors test_fundraising_hardening.py:
  - monkeypatch app.requests.post to fake Shopify GraphQL
  - monkeypatch Stripe module
  - TestClient for HTTP calls
No real network calls are made.
"""
from __future__ import annotations

import json
import datetime
from pathlib import Path
from typing import Any, Dict, List
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import app as studio_app
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_SECRET = "test-admin-secret-payout"
SUPERADMIN_ID = "77777"
REGULAR_ID = "11111"
CRON_SECRET = "test-cron-secret-xyz"


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------

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


def _make_fr_state_gql(state: Dict[str, Any], handle: str = "store-a") -> Dict[str, Any]:
    return {
        "metaobjectByHandle": {
            "id": f"gid://shopify/Metaobject/1",
            "fields": [{"key": "data", "value": json.dumps(state)}],
        }
    }


def _upsert_ok(handle: str = "store-a") -> Dict[str, Any]:
    return {
        "metaobjectUpsert": {
            "metaobject": {"id": "gid://shopify/Metaobject/1", "handle": handle},
            "userErrors": [],
        }
    }


def _all_fundraisers_gql(handles: List[str]) -> Dict[str, Any]:
    """Fake GraphQL response for _fr_all_handles listing multiple stores."""
    return {
        "metaobjects": {
            "edges": [{"node": {"handle": h}} for h in handles],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }


def _admin_headers(customer_id: str | None = None, superadmin: str | None = None) -> dict:
    h = {"X-Admin-Secret": ADMIN_SECRET}
    if customer_id is not None:
        h["X-SS-Customer-Id"] = customer_id
    if superadmin is not None:
        h["X-SS-Superadmin"] = superadmin
    return h


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "shopify-access-token")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("CRON_SECRET", CRON_SECRET)
    monkeypatch.setenv("FUNDRAISING_SUPERADMIN_CUSTOMER_IDS", SUPERADMIN_ID)
    # Reload module-level constants that read env at import time.
    monkeypatch.setattr(studio_app, "_ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setattr(studio_app, "_CRON_SECRET", CRON_SECRET)
    monkeypatch.setattr(studio_app, "_FR_SUPERADMIN_IDS", frozenset([SUPERADMIN_ID]))
    monkeypatch.setattr(studio_app, "_SHOPIFY_SHOP", "example.myshopify.com")
    monkeypatch.setattr(studio_app, "_SHOPIFY_ACCESS_TOKEN", "shopify-access-token")


# ---------------------------------------------------------------------------
# Part A — Auth tests
# ---------------------------------------------------------------------------

def test_summary_403_no_auth(monkeypatch):
    """No credentials at all → 401 (from _require_admin_secret)."""
    client = TestClient(studio_app.app)
    res = client.get("/api/fundraising/payouts/summary")
    assert res.status_code == 401


def test_summary_403_non_superadmin(monkeypatch):
    """Admin secret present but caller is not a super-admin → 403."""
    def fake_post(url, headers=None, json=None, **kwargs):
        return _gql_resp(_all_fundraisers_gql([]))

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    client = TestClient(studio_app.app)
    res = client.get(
        "/api/fundraising/payouts/summary",
        headers=_admin_headers(customer_id=REGULAR_ID),
    )
    assert res.status_code == 403
    assert res.json()["ok"] is False


def test_summary_200_superadmin_via_env_id(monkeypatch):
    """Super-admin via env-var customer id → 200."""
    def fake_post(url, headers=None, json=None, **kwargs):
        return _gql_resp(_all_fundraisers_gql([]))

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    # Stub out Stripe so it doesn't fail.
    monkeypatch.setattr(studio_app, "_STRIPE_KEY", "sk_test_fake")

    class _FakeBalance:
        def get(self, key, default=None):
            return [{"amount": 10000, "currency": "usd"}] if key == "available" else default

    class _FakeStripe:
        class Balance:
            @staticmethod
            def retrieve():
                return _FakeBalance()

    monkeypatch.setattr(studio_app, "_stripe", lambda: _FakeStripe)

    client = TestClient(studio_app.app)
    res = client.get(
        "/api/fundraising/payouts/summary",
        headers=_admin_headers(customer_id=SUPERADMIN_ID),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "next_payday" in body
    assert "stores" in body


def test_summary_200_superadmin_via_header(monkeypatch):
    """Super-admin via X-SS-Superadmin: 1 header → 200."""
    def fake_post(url, headers=None, json=None, **kwargs):
        return _gql_resp(_all_fundraisers_gql([]))

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app, "_STRIPE_KEY", "sk_test_fake")

    class _FakeBalance:
        def get(self, key, default=None):
            return [{"amount": 0, "currency": "usd"}] if key == "available" else default

    class _FakeStripe:
        class Balance:
            @staticmethod
            def retrieve():
                return _FakeBalance()

    monkeypatch.setattr(studio_app, "_stripe", lambda: _FakeStripe)

    client = TestClient(studio_app.app)
    # No customer id in env list — only the header.
    monkeypatch.setattr(studio_app, "_FR_SUPERADMIN_IDS", frozenset())
    res = client.get(
        "/api/fundraising/payouts/summary",
        headers=_admin_headers(superadmin="1"),
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True


# ---------------------------------------------------------------------------
# Part A — Eligibility math
# ---------------------------------------------------------------------------

def _iso(days_ago: float, friday_date: datetime.date) -> str:
    """ISO timestamp N days before the given Friday (UTC)."""
    dt = datetime.datetime(
        friday_date.year, friday_date.month, friday_date.day,
        tzinfo=datetime.timezone.utc,
    ) - datetime.timedelta(days=days_ago)
    return dt.isoformat()


def test_summary_eligibility_math(monkeypatch):
    """
    Per-store eligible_by_friday vs on_hold split is correct relative to
    the computed next Friday.  Also verifies:
      - paid rows are excluded
      - totals and load_needed are correct
      - only stripe_connected stores count toward total_eligible_by_friday
    """
    friday = studio_app._fr_next_friday()

    # store-a: connected, 2 eligible rows ($10+$20), 1 on-hold ($5), 1 paid ($100)
    state_a = {
        "stripe_connected": True,
        "stripe_account_id": "acct_aaa",
        "cause_name": "Baseball",
        "ledger": [
            {"order_id": "1", "amount": 10.0, "paid": False,
             "created_at": _iso(8.0, friday)},   # > 7 days ago → eligible
            {"order_id": "2", "amount": 20.0, "paid": False,
             "created_at": _iso(7.5, friday)},   # > 7 days ago → eligible
            {"order_id": "3", "amount": 5.0,  "paid": False,
             "created_at": _iso(3.0, friday)},   # 3 days ago → on hold
            {"order_id": "4", "amount": 100.0, "paid": True,
             "created_at": _iso(10.0, friday)},  # already paid → excluded
        ],
    }

    # store-b: NOT connected, 1 eligible row ($50)
    state_b = {
        "stripe_connected": False,
        "stripe_account_id": "",
        "cause_name": "Soccer",
        "ledger": [
            {"order_id": "5", "amount": 50.0, "paid": False,
             "created_at": _iso(9.0, friday)},
        ],
    }

    states = {"store-a": state_a, "store-b": state_b}

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        variables = (json or {}).get("variables", {})
        if "metaobjects(" in q:
            return _gql_resp(_all_fundraisers_gql(list(states.keys())))
        if "metaobjectByHandle" in q:
            h = (variables.get("handle") or {}).get("handle", "")
            st = states.get(h, {})
            return _gql_resp(_make_fr_state_gql(st, h))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app, "_STRIPE_KEY", "sk_test_fake")

    stripe_available_usd = 25.0  # $25.00 available

    class _FakeBalance:
        def get(self, key, default=None):
            if key == "available":
                return [{"amount": int(stripe_available_usd * 100), "currency": "usd"}]
            return default

    class _FakeStripe:
        class Balance:
            @staticmethod
            def retrieve():
                return _FakeBalance()

    monkeypatch.setattr(studio_app, "_stripe", lambda: _FakeStripe)

    client = TestClient(studio_app.app)
    res = client.get(
        "/api/fundraising/payouts/summary",
        headers=_admin_headers(customer_id=SUPERADMIN_ID),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True

    # Locate per-store entries.
    store_map = {s["handle"]: s for s in body["stores"]}
    sa = store_map["store-a"]
    sb = store_map["store-b"]

    # store-a: $10 + $20 = $30 eligible; $5 on hold; $35 total_unpaid
    assert sa["eligible_by_friday"] == 30.0
    assert sa["on_hold"] == 5.0
    assert sa["total_unpaid"] == 35.0
    assert sa["eligible_row_count"] == 2
    assert sa["stripe_connected"] is True

    # store-b: $50 eligible (but not connected)
    assert sb["eligible_by_friday"] == 50.0
    assert sb["on_hold"] == 0.0
    assert sb["stripe_connected"] is False

    totals = body["totals"]
    # Only connected stores count toward total_eligible_by_friday
    assert totals["total_eligible_by_friday"] == 30.0
    # Including unconnected
    assert totals["total_eligible_including_unconnected"] == 80.0
    assert totals["total_on_hold"] == 5.0

    # load_needed = max(0, 30.0 - 25.0) = 5.0
    assert body["stripe_available"] == stripe_available_usd
    assert totals["load_needed"] == 5.0

    # next_payday should be a Friday
    payday = datetime.date.fromisoformat(body["next_payday"])
    assert payday.weekday() == 4, "next_payday must be a Friday"


def test_summary_stripe_failure_still_returns_stores(monkeypatch):
    """When stripe.Balance.retrieve() raises, response is 200 with stripe_available=null."""
    state_a = {
        "stripe_connected": True,
        "stripe_account_id": "acct_aaa",
        "cause_name": "Tennis",
        "ledger": [
            {"order_id": "1", "amount": 15.0, "paid": False,
             "created_at": _iso(10.0, studio_app._fr_next_friday())},
        ],
    }

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        variables = (json or {}).get("variables", {})
        if "metaobjects(" in q:
            return _gql_resp(_all_fundraisers_gql(["store-a"]))
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(state_a, "store-a"))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app, "_STRIPE_KEY", "sk_test_fake")

    class _BrokenStripe:
        class Balance:
            @staticmethod
            def retrieve():
                raise RuntimeError("Stripe connection refused")

    monkeypatch.setattr(studio_app, "_stripe", lambda: _BrokenStripe)

    client = TestClient(studio_app.app)
    res = client.get(
        "/api/fundraising/payouts/summary",
        headers=_admin_headers(customer_id=SUPERADMIN_ID),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["stripe_available"] is None
    assert body["stripe_error"] is not None
    assert "Stripe" in body["stripe_error"] or len(body["stripe_error"]) > 0
    # Per-store data must still be present.
    assert len(body["stores"]) == 1
    assert body["stores"][0]["eligible_by_friday"] == 15.0
    # load_needed is null when stripe_available is null.
    assert body["totals"]["load_needed"] is None


# ---------------------------------------------------------------------------
# Part B — Payout safety: idempotency key
# ---------------------------------------------------------------------------

def _make_cron_headers() -> dict:
    return {"X-Cron-Secret": CRON_SECRET}


def test_payout_run_sends_idempotency_key(monkeypatch):
    """stripe.Transfer.create must receive an idempotency_key kwarg."""
    friday = studio_app._fr_next_friday()
    state = {
        "stripe_connected": True,
        "stripe_account_id": "acct_run1",
        "ledger": [
            {"order_id": "ORD-1", "amount": 10.0, "paid": False,
             "created_at": _iso(10.0, friday)},
        ],
    }

    transfer_calls: List[dict] = []
    saved_states: List[dict] = []

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        variables = (json or {}).get("variables", {})
        if "metaobjects(" in q:
            return _gql_resp(_all_fundraisers_gql(["store-run1"]))
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(state, "store-run1"))
        if "metaobjectUpsert" in q:
            fields = (variables.get("metaobject") or {}).get("fields") or []
            for f in fields:
                if f.get("key") == "data":
                    saved_states.append(json_module.loads(f["value"]))
            return _gql_resp(_upsert_ok("store-run1"))
        return _gql_resp({})

    import json as json_module
    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app, "_STRIPE_KEY", "sk_test_fake")

    class _FakeTransfer:
        id = "tr_idempotency_test"

    class _FakeBalance:
        def get(self, key, default=None):
            return [{"amount": 99999, "currency": "usd"}] if key == "available" else default

    class _FakeStripe:
        class Balance:
            @staticmethod
            def retrieve():
                return _FakeBalance()

        class Transfer:
            @staticmethod
            def create(**kwargs):
                transfer_calls.append(kwargs)
                return _FakeTransfer()

    monkeypatch.setattr(studio_app, "_stripe", lambda: _FakeStripe)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/payouts/run",
        headers=_make_cron_headers(),
        json={},
    )
    assert res.status_code == 200, res.text
    assert len(transfer_calls) == 1
    assert "idempotency_key" in transfer_calls[0], "Transfer.create must receive idempotency_key"
    key = transfer_calls[0]["idempotency_key"]
    assert key.startswith("fr-payout:store-run1:"), f"Unexpected key format: {key!r}"


def test_payout_run_no_double_transfer_on_rerun(monkeypatch):
    """
    Re-running payouts/run for the same handle+Friday with the same eligible
    rows must NOT create a second Stripe transfer.
    """
    friday = studio_app._fr_next_friday()
    friday_iso = friday.isoformat()

    # Pre-compute the batch_id that the run will produce so we can seed it
    # as "paid" in the initial state.
    import hashlib
    sorted_ids = ["ORD-2"]
    ids_hash = hashlib.md5("|".join(sorted_ids).encode()).hexdigest()[:12]
    batch_id = f"fr-payout:store-run2:{friday_iso}:{ids_hash}"

    state = {
        "stripe_connected": True,
        "stripe_account_id": "acct_run2",
        "ledger": [
            {"order_id": "ORD-2", "amount": 20.0, "paid": True,
             "transfer_id": "tr_already",
             "created_at": _iso(10.0, friday)},
        ],
        "payout_batches": [
            {
                "batch_id": batch_id,
                "handle": "store-run2",
                "friday": friday_iso,
                "order_ids": sorted_ids,
                "amount": 20.0,
                "status": "paid",
                "transfer_id": "tr_already",
                "created_at": _iso(10.0, friday),
            }
        ],
    }

    transfer_calls: List[dict] = []

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        variables = (json or {}).get("variables", {})
        if "metaobjects(" in q:
            return _gql_resp(_all_fundraisers_gql(["store-run2"]))
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(state, "store-run2"))
        if "metaobjectUpsert" in q:
            return _gql_resp(_upsert_ok("store-run2"))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app, "_STRIPE_KEY", "sk_test_fake")

    class _FakeBalance:
        def get(self, key, default=None):
            return [{"amount": 99999, "currency": "usd"}] if key == "available" else default

    class _FakeStripe:
        class Balance:
            @staticmethod
            def retrieve():
                return _FakeBalance()

        class Transfer:
            @staticmethod
            def create(**kwargs):
                transfer_calls.append(kwargs)

                class _T:
                    id = "tr_second_oops"
                return _T()

    monkeypatch.setattr(studio_app, "_stripe", lambda: _FakeStripe)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/payouts/run",
        headers=_make_cron_headers(),
        json={},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # The ledger row is already paid, so due_cents == 0 → no transfer attempted.
    assert len(transfer_calls) == 0, "No second transfer should be created for already-paid rows"
    result = next((r for r in body["results"] if r["handle"] == "store-run2"), None)
    assert result is not None
    # transferred == 0 means skipped cleanly
    assert result.get("transferred", 0) == 0


def test_payout_run_pending_batch_no_double_transfer(monkeypatch):
    """
    If a pending batch marker exists (transfer was started but second persist
    failed), re-running must reuse the idempotency key and NOT create a second
    Stripe transfer call (relies on Stripe idempotency; our code calls create
    once and Stripe deduplicates via key).
    """
    friday = studio_app._fr_next_friday()
    friday_iso = friday.isoformat()

    import hashlib
    sorted_ids = ["ORD-3"]
    ids_hash = hashlib.md5("|".join(sorted_ids).encode()).hexdigest()[:12]
    batch_id = f"fr-payout:store-run3:{friday_iso}:{ids_hash}"

    # State has the pending marker (first run crashed before 2nd persist) but
    # ledger row is still unpaid.
    state = {
        "stripe_connected": True,
        "stripe_account_id": "acct_run3",
        "ledger": [
            {"order_id": "ORD-3", "amount": 30.0, "paid": False,
             "created_at": _iso(10.0, friday)},
        ],
        "payout_batches": [
            {
                "batch_id": batch_id,
                "handle": "store-run3",
                "friday": friday_iso,
                "order_ids": sorted_ids,
                "amount": 30.0,
                "status": "pending",
                "created_at": _iso(0.1, friday),
            }
        ],
    }

    transfer_calls: List[dict] = []
    saved_states: List[dict] = []

    def fake_post(url, headers=None, json=None, **kwargs):
        q = (json or {}).get("query", "")
        variables = (json or {}).get("variables", {})
        if "metaobjects(" in q:
            return _gql_resp(_all_fundraisers_gql(["store-run3"]))
        if "metaobjectByHandle" in q:
            return _gql_resp(_make_fr_state_gql(state, "store-run3"))
        if "metaobjectUpsert" in q:
            fields = (variables.get("metaobject") or {}).get("fields") or []
            for f in fields:
                if f.get("key") == "data":
                    import json as _j
                    saved_states.append(_j.loads(f["value"]))
            return _gql_resp(_upsert_ok("store-run3"))
        return _gql_resp({})

    monkeypatch.setattr(studio_app.requests, "post", fake_post)
    monkeypatch.setattr(studio_app, "_STRIPE_KEY", "sk_test_fake")

    class _FakeBalance:
        def get(self, key, default=None):
            return [{"amount": 99999, "currency": "usd"}] if key == "available" else default

    class _FakeStripe:
        class Balance:
            @staticmethod
            def retrieve():
                return _FakeBalance()

        class Transfer:
            @staticmethod
            def create(**kwargs):
                transfer_calls.append(kwargs)

                class _T:
                    id = "tr_idempotent_retry"
                return _T()

    monkeypatch.setattr(studio_app, "_stripe", lambda: _FakeStripe)

    client = TestClient(studio_app.app)
    res = client.post(
        "/api/fundraising/payouts/run",
        headers=_make_cron_headers(),
        json={},
    )
    assert res.status_code == 200, res.text

    # Exactly one Transfer.create call (the retry), with the same idempotency key.
    assert len(transfer_calls) == 1
    assert transfer_calls[0]["idempotency_key"] == batch_id

    # The final saved state should have the batch marked "paid".
    assert len(saved_states) >= 1
    final = saved_states[-1]
    batch = next((b for b in (final.get("payout_batches") or []) if b["batch_id"] == batch_id), None)
    assert batch is not None, "batch record must be present in persisted state"
    assert batch["status"] == "paid"
    assert batch.get("transfer_id") == "tr_idempotent_retry"


# ---------------------------------------------------------------------------
# Part A — next_payday is always a Friday
# ---------------------------------------------------------------------------

def test_next_friday_is_friday():
    """_fr_next_friday() always returns a date with weekday()==4."""
    import datetime as dt
    # Test across all 7 days of the week.
    # Use a known Monday: 2026-06-15 (weekday 0)
    monday = dt.date(2026, 6, 15)
    for offset in range(7):
        d = monday + dt.timedelta(days=offset)
        nf = studio_app._fr_next_friday(d)
        assert nf.weekday() == 4, f"Expected Friday for input {d}, got {nf} (weekday {nf.weekday()})"
        assert nf >= d, "next_friday must not be in the past"
        # Advance must be at most 6 days.
        assert (nf - d).days <= 6


def test_next_friday_on_friday_is_same_day():
    """If today is Friday, _fr_next_friday returns today."""
    import datetime as dt
    friday = dt.date(2026, 6, 19)  # Known Friday
    assert friday.weekday() == 4
    assert studio_app._fr_next_friday(friday) == friday
