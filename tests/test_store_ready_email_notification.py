from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as studio_app
from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_store_ready_starts_notification_thread_with_store_name(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "token")

    responses = [
        _FakeResponse(
            {
                "data": {
                    "metaobjectByHandle": {
                        "id": "gid://shopify/Metaobject/1",
                        "fields": [
                            {"key": "name", "value": "Alpha Store"},
                            {"key": "is_fully_ready", "value": "false"},
                        ],
                    }
                }
            }
        ),
        _FakeResponse({"data": {"metaobjectUpdate": {"userErrors": []}}}),
    ]

    def _fake_post(*args, **kwargs):
        return responses.pop(0)

    thread_calls = []

    class _FakeThread:
        def __init__(self, target, args, daemon):
            thread_calls.append({"target": target, "args": args, "daemon": daemon, "started": False})
            self._idx = len(thread_calls) - 1

        def start(self):
            thread_calls[self._idx]["started"] = True

    monkeypatch.setattr(studio_app.requests, "post", _fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", _FakeThread)

    client = TestClient(studio_app.app)
    res = client.post(
        "/store/alpha/store-ready",
        headers={"X-Admin-Secret": "stellasage-god-mode-2026-xK9mP"},
    )

    assert res.status_code == 200
    assert res.json() == {"status": "ok", "handle": "alpha", "is_fully_ready": True}
    assert len(thread_calls) == 1
    assert thread_calls[0]["target"] is studio_app._send_new_store_email
    assert thread_calls[0]["args"] == ("alpha", "Alpha Store")
    assert thread_calls[0]["daemon"] is True
    assert thread_calls[0]["started"] is True


def test_store_ready_uses_handle_as_store_name_fallback(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "token")

    responses = [
        _FakeResponse(
            {
                "data": {
                    "metaobjectByHandle": {
                        "id": "gid://shopify/Metaobject/1",
                        "fields": [{"key": "status", "value": "building"}],
                    }
                }
            }
        ),
        _FakeResponse({"data": {"metaobjectUpdate": {"userErrors": []}}}),
    ]

    def _fake_post(*args, **kwargs):
        return responses.pop(0)

    thread_args = []

    class _FakeThread:
        def __init__(self, target, args, daemon):
            thread_args.append(args)

        def start(self):
            return None

    monkeypatch.setattr(studio_app.requests, "post", _fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", _FakeThread)

    client = TestClient(studio_app.app)
    res = client.post(
        "/store/fallback-handle/store-ready",
        headers={"X-Admin-Secret": "stellasage-god-mode-2026-xK9mP"},
    )

    assert res.status_code == 200
    assert thread_args == [("fallback-handle", "fallback-handle")]


def test_store_ready_skips_notification_thread_on_update_errors(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "token")

    responses = [
        _FakeResponse(
            {
                "data": {
                    "metaobjectByHandle": {
                        "id": "gid://shopify/Metaobject/1",
                        "fields": [{"key": "name", "value": "Alpha Store"}],
                    }
                }
            }
        ),
        _FakeResponse(
            {
                "data": {
                    "metaobjectUpdate": {
                        "userErrors": [{"field": ["status"], "message": "Invalid value"}]
                    }
                }
            }
        ),
    ]

    def _fake_post(*args, **kwargs):
        return responses.pop(0)

    thread_calls = []

    class _FakeThread:
        def __init__(self, target, args, daemon):
            thread_calls.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(studio_app.requests, "post", _fake_post)
    monkeypatch.setattr(studio_app.threading, "Thread", _FakeThread)

    client = TestClient(studio_app.app)
    res = client.post(
        "/store/alpha/store-ready",
        headers={"X-Admin-Secret": "stellasage-god-mode-2026-xK9mP"},
    )

    assert res.status_code == 500
    assert thread_calls == []
