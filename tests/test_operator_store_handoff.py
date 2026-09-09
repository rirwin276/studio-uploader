"""The founder manages stores without taking the first customer ownership slot.

Exercise the existing public form endpoint and authenticated join endpoint;
all Shopify reads/writes and build jobs are replaced with controlled fixtures.
"""
import importlib
import os
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SHOP", "example.myshopify.com")
os.environ.setdefault("API_VERSION", "2026-01")
os.environ.setdefault("CLIENT_SECRET", "test-token")
core = importlib.import_module("app")


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(core, "_ADMIN_SECRET", "test-secret")
    tags = Mock(return_value=[])
    store = Mock(return_value=None)
    job = Mock()
    thread = Mock()
    monkeypatch.setattr(core, "_get_customer_tags", tags)
    monkeypatch.setattr(core, "_get_custom_shop", store)
    monkeypatch.setattr(core, "_job_set", job)
    monkeypatch.setattr(core.threading, "Thread", thread)
    monkeypatch.setattr(
        core.requests.sessions.Session, "request",
        Mock(side_effect=AssertionError("Unexpected external HTTP request")),
    )
    return TestClient(core.app), tags, store, job, thread


def submit(client, **changes):
    data = {
        "customer_id": "101",
        "customer_email": "creator@example.org",
        "storefront_name": "Handoff Test",
        "storefront_handle": "handoff-test",
        "main_session_id": "fixture-session",
        "primary_color": "Navy",
        "type_of_store_direct": "soccer",
    }
    data.update(changes)
    return client.post("/api/storefront-request", data=data)


@pytest.mark.parametrize("operator_tags", [["super-admin"], [" Super-Admin ", "storefront-admin--another"]])
def test_normal_form_operator_build_leaves_customer_owner_unclaimed(boundary, operator_tags):
    client, tags, store, job, thread = boundary
    tags.return_value = operator_tags
    response = submit(client, customer_id="gid://shopify/Customer/101")
    assert response.status_code == 200
    tags.assert_called_once_with("gid://shopify/Customer/101")
    store.assert_called_once_with("handoff-test")
    assert job.call_args.kwargs["owner_customer_id"] == ""
    assert job.call_args.kwargs["claimable"] is True
    # Existing provisioning receives the same image, type/color and worker.
    assert thread.call_args.kwargs["target"] is core._run_shopify_provision_job
    args = thread.call_args.kwargs["args"]
    assert args[1:8] == ("Handoff Test", "handoff-test", "", "soccer", "Navy", "fixture-session", None)
    thread.return_value.start.assert_called_once()


@pytest.mark.parametrize("customer_tags", [[], ["storefront-admin--another"], ["b2b-admin-another"]])
def test_customers_create_their_own_store_as_owner(boundary, customer_tags):
    client, tags, store, job, thread = boundary
    tags.return_value = customer_tags
    assert submit(client).status_code == 200
    assert job.call_args.kwargs["owner_customer_id"] == "101"
    assert job.call_args.kwargs["claimable"] is False
    assert thread.call_args.kwargs["args"][3] == "101"
    store.assert_not_called()


def test_browser_role_flags_do_not_make_an_operator(boundary):
    client, tags, store, job, thread = boundary
    response = submit(client, super_admin="1", platform_operator="1")
    assert response.status_code == 200
    assert job.call_args.kwargs["owner_customer_id"] == "101"
    assert job.call_args.kwargs["claimable"] is False


@pytest.mark.parametrize("owner", ["202", "101", "unclaimed"])
def test_operator_duplicate_build_never_resets_an_existing_store(boundary, owner):
    client, tags, store, job, thread = boundary
    tags.return_value = ["super-admin"]
    store.return_value = {"id": "store-1", "fields": {"owner_customer_id": owner}}
    assert submit(client).status_code == 409
    job.assert_not_called()
    thread.assert_not_called()


def test_role_lookup_failure_starts_no_job_instead_of_guessing_owner(boundary):
    client, tags, store, job, thread = boundary
    tags.side_effect = RuntimeError("Shopify unavailable")
    assert submit(client).status_code == 502
    job.assert_not_called()
    thread.assert_not_called()


def test_missing_customer_starts_no_job(boundary):
    client, tags, store, job, thread = boundary
    tags.return_value = None
    assert submit(client).status_code == 404
    job.assert_not_called()
    thread.assert_not_called()


def test_existing_store_lookup_failure_cannot_unclaim_it(boundary):
    client, tags, store, job, thread = boundary
    tags.return_value = ["super-admin"]
    store.side_effect = RuntimeError("Shopify unavailable")
    assert submit(client).status_code == 502
    job.assert_not_called()
    thread.assert_not_called()


def test_explicit_claimable_api_still_requires_its_existing_auth(boundary):
    client, tags, store, job, thread = boundary
    assert submit(client, claimable="true").status_code == 401
    tags.assert_not_called()
    job.assert_not_called()
    thread.assert_not_called()


@pytest.mark.parametrize("owner", ["unclaimed", "202"])
def test_operator_join_never_claims_or_changes_owner(boundary, monkeypatch, owner):
    client, tags, store, job, thread = boundary
    tags.return_value = ["super-admin", "storefront-admin--handoff-test"]
    store.return_value = {
        "id": "store-1",
        "fields": {"owner_customer_id": owner, "collection_gid": "collection-1"},
    }
    forbidden = {}
    for key in ("_get_collection_claim_owner", "_try_create_collection_claim", "_set_custom_shop_owner", "_customer_add_tag"):
        forbidden[key] = Mock(side_effect=AssertionError("Operator must not consume/change a customer claim"))
        monkeypatch.setattr(core, key, forbidden[key])
    response = client.post("/api/storefront/handoff-test/join", json={"customer_id": "101"}, headers={"X-Admin-Secret": "test-secret"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["claimed_admin"] is False
    assert response.json()["platform_operator"] is True
    for fn in forbidden.values():
        fn.assert_not_called()


def test_operator_then_first_customer_then_member_lifecycle(boundary, monkeypatch):
    client, tags, store, job, thread = boundary
    state = {"id": "store-1", "fields": {"owner_customer_id": "unclaimed", "collection_gid": "collection-1"}}
    tags_by_id = {"101": ["super-admin"], "202": [], "303": []}
    tags.side_effect = lambda gid: tags_by_id[gid.split("/")[-1]]
    store.return_value = state
    marker = {"owner": ""}
    monkeypatch.setattr(core, "_get_collection_claim_owner", lambda gid: marker["owner"])

    def claim(gid, customer_id):
        if marker["owner"]:
            return False
        marker["owner"] = customer_id
        return True

    monkeypatch.setattr(core, "_try_create_collection_claim", claim)
    monkeypatch.setattr(core, "_set_custom_shop_owner", lambda gid, cid: state["fields"].update(owner_customer_id=cid))
    monkeypatch.setattr(core, "_customer_add_tag", lambda gid, tag: tags_by_id[gid.split("/")[-1]].append(tag))
    import prospect_demo
    claimed = Mock()
    monkeypatch.setattr(prospect_demo, "mark_claimed", claimed)

    def join(cid):
        response = client.post("/api/storefront/handoff-test/join", json={"customer_id": cid}, headers={"X-Admin-Secret": "test-secret"})
        assert response.status_code == 200
        return response.json()

    assert join("101")["claimed_admin"] is False
    assert marker["owner"] == ""
    assert state["fields"]["owner_customer_id"] == "unclaimed"
    assert join("202")["claimed_admin"] is True
    assert state["fields"]["owner_customer_id"] == marker["owner"] == "202"
    assert join("303")["role"] == "member"
    assert tags_by_id["202"] == ["storefront-admin--handoff-test", "storefront-member--handoff-test"]
    assert tags_by_id["303"] == ["storefront-member--handoff-test"]
    assert join("101")["platform_operator"] is True
    assert state["fields"]["owner_customer_id"] == "202"
    assert tags_by_id["101"] == ["super-admin"]
    claimed.assert_called_once()


def test_operator_join_still_requires_authenticated_relay(boundary):
    client, tags, store, job, thread = boundary
    tags.return_value = ["super-admin"]
    response = client.post("/api/storefront/handoff-test/join", json={"customer_id": "101"})
    assert response.status_code == 401
    tags.assert_not_called()
