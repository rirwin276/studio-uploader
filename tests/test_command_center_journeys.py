import copy
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import command_center as cc
import command_center_fast as fast
import site_sessions as sessions


IDENTITY = {"owner": False, "bot": False, "role_unknown": False, "signed_in": False, "stores": []}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(sessions, "_list_cache", None)
    monkeypatch.setattr(sessions, "_definition_ready", False)
    monkeypatch.setattr(fast, "_directory", None)
    monkeypatch.setattr(fast, "_running", False)
    monkeypatch.setattr(fast, "_last_attempt", 0)
    monkeypatch.setattr(cc, "_SUMMARY_CACHE", None)


def body(**kwargs):
    return {"session_id": "session-12345", "timezone": "America/Los_Angeles", "pages": [
        {"id": "page-12345", "path": "/pages/private-storefronts?email=secret@example.com#pin", "active_seconds": 0,
         "events": ["page_view", "create_opened"]}], **kwargs}


def test_journey_deduplicates_heartbeats_and_keeps_paths_without_private_query():
    row = sessions._merge({}, body(), IDENTITY, "2026-09-11T10:00:00+00:00")
    data = body()
    data["pages"][0]["active_seconds"] = 30
    data["pages"][0]["events"].append("create_started")
    row = sessions._merge(row, data, IDENTITY, "2026-09-11T10:00:30+00:00")
    row = sessions._merge(row, data, IDENTITY, "2026-09-11T10:00:31+00:00")
    assert len(row["pages"]) == 1
    assert row["active_seconds"] == 30
    assert row["duration_seconds"] == 31
    assert "secret" not in json.dumps(row)
    assert row["location"] is None
    assert sessions._path("/checkouts/sensitive-token") == "/checkout"
    assert sessions._path("https://evil.example/path") == "/"


def test_owner_sign_in_excludes_whole_anonymous_journey_and_stays_excluded():
    row = sessions._merge({}, body(), IDENTITY, "2026-09-11T10:00:00+00:00")
    owner = {**IDENTITY, "owner": True, "signed_in": True}
    row = sessions._merge(row, body(), owner, "2026-09-11T10:01:00+00:00")
    row = sessions._merge(row, body(), IDENTITY, "2026-09-11T10:02:00+00:00")
    assert not sessions.visible(row)
    bot = sessions._merge({}, body(), {**IDENTITY, "bot": True}, "2026-09-11T10:00:00+00:00")
    assert not sessions.visible(bot)
    unknown = sessions._merge({}, body(), {**IDENTITY, "role_unknown": True, "signed_in": True}, "2026-09-11T10:00:00+00:00")
    assert not sessions.visible(unknown)
    assert not sessions.visible(sessions._merge(unknown, body(), IDENTITY, "2026-09-11T10:01:00+00:00"))


def test_membership_is_from_verified_identity_not_browser_fields():
    data = body(store_handles=["forged"], signed_in=True)
    row = sessions._merge({}, data, {**IDENTITY, "signed_in": True, "stores": ["real-team"]}, "2026-09-11T10:00:00+00:00")
    assert row["store_handles"] == ["real-team"]
    assert row["pages"][0]["signed_in"] is True


def test_funnel_does_not_treat_open_sessions_or_accepted_requests_as_dropoffs():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    base = sessions._merge({}, body(), IDENTITY, old)
    base["id"] = "a" * 64
    accepted = copy.deepcopy(base)
    accepted["events"].append("create_accepted")
    recent = {**base, "last_seen_at": now.isoformat()}
    excluded = {**base, "excluded": True}
    result = sessions.summarize([base, accepted, recent, excluded], True)
    assert result["sample_size"] == 3
    assert result["funnel"]["create_opened"] == 3
    assert result["funnel"]["create_accepted"] == 1
    assert result["exit_pages"] == [("/pages/private-storefronts", 1)]
    assert result["truncated"]
    assert all("pages" not in s for s in result["sessions"])


class Core:
    log = logging.getLogger("test")
    def _require_admin_secret(self, request):
        if request.headers.get("X-Admin-Secret") != "test-secret":
            return JSONResponse({"ok": False}, status_code=403)


def client():
    app = FastAPI()
    fast.install_fast_routes(app, Core())
    sessions.install_session_routes(app, Core())
    return TestClient(app)


def test_admin_routes_require_both_private_secret_and_verified_owner():
    c = client()
    for path in ["index", "report", "store/team-one", "sessions", "sessions/" + "a" * 64]:
        for headers in [{}, {"X-SS-Superadmin": "1"}, {"X-Admin-Secret": "test-secret"}]:
            assert c.get("/admin/command-center/" + path, headers=headers).status_code == 403


def test_directory_returns_all_stores_without_waiting_for_heavy_snapshot(monkeypatch):
    release = threading.Event()
    def build(core):
        assert release.wait(3)
        return {"stores": [], "generated_at": "now"}
    monkeypatch.setattr(cc, "_build_summary", build)
    monkeypatch.setattr(cc, "_store_nodes", lambda core: [{"handle": "team-" + str(i), "fields": []} for i in range(251)])
    try:
        response = client().get("/admin/command-center/index", headers={"X-Admin-Secret": "test-secret", "X-SS-Superadmin": "1"})
        assert response.status_code == 200
        assert len(response.json()["stores"]) == 251
        assert response.json()["refreshing"]
        assert not response.json()["metrics_ready"]
    finally:
        release.set()
        # Join the bounded test worker so cache state cannot leak into another test.
        for worker in threading.enumerate():
            if worker.name == "command-center-refresh":
                worker.join(3)


def test_index_omits_detail_arrays_and_ranks_latest_engagement():
    row = fast._compact({"handle":"one", "customers":{"store_admins":[{"email":"private"}],"total":1},
        "activity":{"recent_sessions":[{}],"last_authenticated_customer_activity":{"at":"2026-09-10T00:00:00Z"}},
        "sales":{"recent_purchases":[{}],"last_purchase":{"created_at":"2026-09-11T00:00:00Z"}}})
    assert "private" not in json.dumps(row)
    assert "recent_purchases" not in row["sales"]
    assert "recent_sessions" not in row["activity"]
    assert row["last_engagement_at"] == "2026-09-11T00:00:00Z"


def test_session_api_rejects_invalid_bodies_without_shopify_calls():
    c = client()
    headers = {"X-Admin-Secret":"test-secret"}
    for data in [[], {"session_id":"short"}, {"session_id":"valid-id-123", "pages":None}]:
        assert c.post("/api/activity/session",json=data,headers=headers).status_code == 400
    assert c.post("/api/activity/session",content="x"*32001,headers=headers).status_code == 413
