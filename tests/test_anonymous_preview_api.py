from types import SimpleNamespace
import pytest
from fastapi import HTTPException
import anonymous_preview_api as api


@pytest.mark.parametrize("owner", ["", "unclaimed", "__anonymous_preview_deleting__"])
def test_unclaimed_or_deleting_products_never_activate(owner):
    core = SimpleNamespace(_normalize_store_owner=lambda x: "" if x == "unclaimed" else x)
    assert api.activate_claimed(core, "team-demo-abc123", {"fields":{"owner_customer_id":owner}}) is False


def test_only_claimed_visible_products_activate(monkeypatch):
    calls = []
    core = SimpleNamespace(_normalize_store_owner=lambda x:x)
    monkeypatch.setattr(api.outreach_tracking,"read",lambda *a:{})
    monkeypatch.setattr(api.outreach_tracking,"update",lambda *a:calls.append(("state",a[-1])))
    monkeypatch.setattr(api,"products",lambda *a:[{"id":"visible","tags":[api.MARKER],"status":"DRAFT"},{"id":"hidden","tags":[api.MARKER,api.HIDDEN],"status":"DRAFT"}])
    monkeypatch.setattr(api,"mutate",lambda c,q,v,f:calls.append((f,v)))
    assert api.activate_claimed(core,"team-demo-abc123",{"fields":{"owner_customer_id":"123","is_fully_ready":"true"}})
    updates=[v for kind,v in calls if kind=="productUpdate"]
    assert updates == [{"input":{"id":"visible","status":"ACTIVE"}}]


def test_product_search_results_require_exact_store_and_marker():
    core=SimpleNamespace(_shopify_graphql=lambda *a:{"products":{"nodes":[
        {"id":"ours","tags":["team-demo-abc123",api.MARKER]},
        {"id":"another","tags":["other-demo-abc123",api.MARKER]},
        {"id":"not-preview","tags":["team-demo-abc123"]}],"pageInfo":{"hasNextPage":False}}})
    assert [p["id"] for p in api.products(core,"team-demo-abc123")] == ["ours"]


def test_bad_bearer_denied_before_shopify(monkeypatch):
    monkeypatch.setattr(api.demo,"_verify_token",lambda t: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(HTTPException) as exc:
        api.authorize(SimpleNamespace(),SimpleNamespace(headers={}))
    assert exc.value.status_code == 401


def test_building_design_delays_activation(monkeypatch):
    core = SimpleNamespace(_normalize_store_owner=lambda x:x)
    monkeypatch.setattr(api.outreach_tracking,"read",lambda *a:{"prospect_demo":{"product_status":"building"}})
    assert not api.activate_claimed(core,"team-demo-abc123",{"fields":{"owner_customer_id":"123","is_fully_ready":"true"}})
