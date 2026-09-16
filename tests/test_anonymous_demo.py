from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import anonymous_demo
import outreach_tracking


class ImmediateThread:
    def __init__(self, *, target, kwargs=None, **_ignored):
        self.target = target
        self.kwargs = kwargs or {}

    def start(self):
        self.target(**self.kwargs)


class FakeCore:
    MAX_UPLOAD_BYTES = 1024 * 1024

    def __init__(self):
        self.jobs = {}
        self.tagged = []
        self.activated = []
        self.ready = True

    def _get_custom_shop(self, _handle):
        return {"fields": {"is_fully_ready": "true" if self.ready else "false"}}

    def _job_set(self, job_id, **patch):
        self.jobs.setdefault(job_id, {}).update(patch)

    def _job_get(self, job_id):
        return dict(self.jobs.get(job_id, {}))

    def _run_shopify_provision_job(self, job_id, *_args, **_kwargs):
        self._job_set(job_id, status="succeeded")

    def _shopify_graphql(self, query, variables):
        if "AnonymousDemoProducts" in query or "AnonymousStoreProducts" in query:
            return {
                "products": {
                    "nodes": [{"id": "gid://shopify/Product/1", "status": "DRAFT", "tags": ["raptors-demo-a1b2c3"]}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        if "TagAnonymousDemoProduct" in query:
            self.tagged.append(variables["id"])
            return {"tagsAdd": {"node": {"id": variables["id"]}, "userErrors": []}}
        raise AssertionError(query)

    def _shopify_rest_put(self, path, body):
        self.activated.append((path, body))
        return body


def test_capacity_counts_only_unexpired_builds(monkeypatch):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    states = {
        "building": {"source": "anonymous_demo", "status": "anonymous_building", "claim_status": "unclaimed", "expires_at": future},
        "finished": {"source": "anonymous_demo", "status": "ready", "claim_status": "unclaimed", "expires_at": future},
        "claimed": {"source": "anonymous_demo", "status": "building", "claim_status": "claimed", "expires_at": future},
        "stale": {"source": "anonymous_demo", "status": "queued", "claim_status": "unclaimed", "expires_at": past},
        "other": {"source": "direct_outreach_api", "status": "building", "claim_status": "unclaimed", "expires_at": future},
    }
    monkeypatch.setattr(anonymous_demo.outreach_tracking, "list_all", lambda _core: states)
    monkeypatch.setenv("ANONYMOUS_DEMO_MAX_ACTIVE", "1")

    assert anonymous_demo._active_count(object()) == 1
    assert anonymous_demo._at_capacity(object()) is True

    states["building"]["status"] = "ready"
    assert anonymous_demo._active_count(object()) == 0
    assert anonymous_demo._at_capacity(object()) is False


def _setup(monkeypatch):
    import anonymous_preview_api
    monkeypatch.setattr(anonymous_preview_api, "builder_ready", lambda: True)
    monkeypatch.setenv("ANONYMOUS_DEMO_ENABLED", "true")
    monkeypatch.setenv("ANONYMOUS_DEMO_SECRET", "test-secret-that-is-at-least-thirty-two-bytes")
    monkeypatch.setattr(anonymous_demo, "_rate_allowed", lambda _request: True)
    monkeypatch.setattr(anonymous_demo, "_at_capacity", lambda _core: False)
    monkeypatch.setattr(anonymous_demo, "_unique_handle", lambda _core, _name: "raptors-demo-a1b2c3")

    async def save_logo(_core, _upload, _handle):
        return "anonymous-logo-session"

    monkeypatch.setattr(anonymous_demo, "_save_logo_session", save_logo)
    monkeypatch.setattr(anonymous_demo.outreach_appearance, "apply", lambda _core, _handle: None)
    monkeypatch.setattr(anonymous_demo.threading, "Thread", ImmediateThread)

    states = {}

    def read(_core, handle):
        return dict(states.get(handle, {}))

    def upsert(_core, handle, state):
        states[handle] = dict(state)

    def update(_core, handle, patch):
        state = dict(states.get(handle, {}))
        state.update(patch)
        states[handle] = state
        return dict(state)

    monkeypatch.setattr(anonymous_demo.outreach_tracking, "read", read)
    monkeypatch.setattr(anonymous_demo.outreach_tracking, "upsert", upsert)
    monkeypatch.setattr(anonymous_demo.outreach_tracking, "update", update)

    app = FastAPI()
    core = FakeCore()
    anonymous_demo.install_anonymous_demo_routes(app, core)
    return TestClient(app), core, states


def test_feature_flag_fails_closed(monkeypatch):
    monkeypatch.delenv("ANONYMOUS_DEMO_ENABLED", raising=False)
    app = FastAPI()
    anonymous_demo.install_anonymous_demo_routes(app, FakeCore())
    response = TestClient(app).post(
        "/api/demo/storefront-request",
        data={"storefront_name": "Raptors"},
        files={"storefront_logo_file": ("logo.png", b"x", "image/png")},
    )
    assert response.status_code == 404


def test_start_builds_ownerless_demo_and_returns_secure_resume_token(monkeypatch):
    client, core, states = _setup(monkeypatch)
    response = client.post(
        "/api/demo/storefront-request",
        headers={"Origin": "https://stellasageco.com"},
        data={
            "storefront_name": "Raptors Baseball",
            "type_of_store": "youth-baseball",
            "primary_color": "Navy",
        },
        files={"storefront_logo_file": ("logo.png", b"fake-image", "image/png")},
    )
    assert response.status_code == 200
    body = response.json()
    token = body["resume_token"]
    assert token not in str(states)

    state = states["raptors-demo-a1b2c3"]
    assert state["source"] == "anonymous_demo"
    assert state["store_status"] == "anonymous_demo_unclaimed"
    assert state["claim_status"] == "unclaimed"
    assert state["status"] == "ready"
    assert state["resume_token_hash"] == anonymous_demo._token_hash(token)
    assert outreach_tracking.parse_iso(state["expires_at"]).timestamp() - outreach_tracking.parse_iso(state["ready_at"]).timestamp() == 48 * 60 * 60
    assert core.tagged == ["gid://shopify/Product/1"]
    assert core.activated[0][0] == "products/1.json"

    denied = client.get("/api/demo/status")
    assert denied.status_code == 401
    status = client.get("/api/demo/status", headers={"Authorization": f"Bearer {token}"})
    assert status.status_code == 200
    assert status.json()["phase"] == "ready"
    assert status.json()["admin_url"].endswith("shop=raptors-demo-a1b2c3&prospect_demo=1")


def test_return_token_cannot_be_swapped_between_stores(monkeypatch):
    client, _core, states = _setup(monkeypatch)
    response = client.post(
        "/api/demo/storefront-request",
        data={"storefront_name": "Raptors"},
        files={"storefront_logo_file": ("logo.png", b"fake", "image/png")},
    )
    token = response.json()["resume_token"]
    states["raptors-demo-a1b2c3"]["resume_token_hash"] = "0" * 64
    denied = client.get("/api/demo/status", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 401


def test_honeypot_and_wrong_origin_do_not_start_builds(monkeypatch):
    client, core, states = _setup(monkeypatch)
    wrong_origin = client.post(
        "/api/demo/storefront-request",
        headers={"Origin": "https://attacker.example"},
        data={"storefront_name": "Raptors"},
        files={"storefront_logo_file": ("logo.png", b"fake", "image/png")},
    )
    assert wrong_origin.status_code == 403
    honeypot = client.post(
        "/api/demo/storefront-request",
        data={"storefront_name": "Raptors", "website": "https://spam.example"},
        files={"storefront_logo_file": ("logo.png", b"fake", "image/png")},
    )
    assert honeypot.status_code == 400
    assert states == {}
    assert core.jobs == {}


def test_status_remains_available_when_new_demo_entry_is_disabled(monkeypatch):
    client, _core, _states = _setup(monkeypatch)
    response = client.post(
        "/api/demo/storefront-request",
        data={"storefront_name": "Raptors"},
        files={"storefront_logo_file": ("logo.png", b"fake", "image/png")},
    )
    token = response.json()["resume_token"]
    monkeypatch.setenv("ANONYMOUS_DEMO_ENABLED", "false")
    status = client.get("/api/demo/status", headers={"Authorization": f"Bearer {token}"})
    assert status.status_code == 200
    assert status.json()["phase"] == "ready"



def test_waits_for_real_products_then_marks_ready_once(monkeypatch):
    client, core, states = _setup(monkeypatch)
    core.ready = False
    response = client.post('/api/demo/storefront-request', data={'storefront_name':'Raptors'}, files={'storefront_logo_file':('logo.png',b'fake','image/png')})
    headers = {'Authorization': 'Bearer ' + response.json()['resume_token']}
    first = client.get('/api/demo/status', headers=headers).json()
    assert first['phase'] == 'building'
    assert first['build_stage'] == 'products'
    assert 'preview_url' not in first
    assert core.tagged == []
    core.ready = True
    ready = client.get('/api/demo/status', headers=headers).json()
    assert ready['phase'] == 'ready'
    assert core.tagged == ['gid://shopify/Product/1']
    again = client.get('/api/demo/status', headers=headers)
    assert again.json()['ready_at'] == ready['ready_at']
    assert again.headers['cache-control'] == 'no-store'


def test_readiness_failure_does_not_expose_store(monkeypatch):
    client, core, states = _setup(monkeypatch)
    core.ready = False
    response = client.post('/api/demo/storefront-request', data={'storefront_name':'Raptors'}, files={'storefront_logo_file':('logo.png',b'fake','image/png')})
    core.ready = True
    monkeypatch.setattr(anonymous_demo, '_activate_store_products', lambda *_: (_ for _ in ()).throw(RuntimeError('activation failed')))
    response = client.get('/api/demo/status', headers={'Authorization':'Bearer '+response.json()['resume_token']})
    assert response.status_code == 503
    assert states['raptors-demo-a1b2c3']['status'] == 'anonymous_building'


def test_recovers_preview_reclassified_by_legacy_intake_worker(monkeypatch):
    client, core, states = _setup(monkeypatch)
    core.ready = False
    response = client.post('/api/demo/storefront-request', data={'storefront_name':'Raptors'}, files={'storefront_logo_file':('logo.png',b'fake','image/png')})
    state = states['raptors-demo-a1b2c3']
    assert state['status'] == 'anonymous_building'
    state.update(status='provisioned', store_status='prospect_unclaimed')
    core.ready = True
    result = client.get('/api/demo/status', headers={'Authorization':'Bearer '+response.json()['resume_token']})
    assert result.json()['phase'] == 'ready'
    assert states['raptors-demo-a1b2c3']['store_status'] == 'anonymous_demo_unclaimed'
