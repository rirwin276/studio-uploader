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
    # _ADMIN_SECRET is captured at import time and fails closed when unset.
    monkeypatch.setattr(studio_app, "_ADMIN_SECRET", "stellasage-god-mode-2026-xK9mP")

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
    assert len(thread_calls) == 2
    assert thread_calls[0]["target"] is studio_app._send_new_store_email
    assert thread_calls[0]["args"] == ("alpha", "Alpha Store")
    assert thread_calls[0]["daemon"] is True
    assert thread_calls[0]["started"] is True
    # The social post is drafted on its own thread so it can't affect store-ready.
    assert thread_calls[1]["target"] is studio_app._queue_social_engine_post
    assert thread_calls[1]["args"] == ("alpha", "Alpha Store")
    assert thread_calls[1]["daemon"] is True
    assert thread_calls[1]["started"] is True


def test_store_ready_uses_handle_as_store_name_fallback(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "token")
    # _ADMIN_SECRET is captured at import time and fails closed when unset.
    monkeypatch.setattr(studio_app, "_ADMIN_SECRET", "stellasage-god-mode-2026-xK9mP")

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
    assert thread_args == [
        ("fallback-handle", "fallback-handle"),
        ("fallback-handle", "fallback-handle"),
    ]


def test_store_ready_skips_notification_thread_on_update_errors(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "token")
    # _ADMIN_SECRET is captured at import time and fails closed when unset.
    monkeypatch.setattr(studio_app, "_ADMIN_SECRET", "stellasage-god-mode-2026-xK9mP")

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


def test_queue_social_engine_post_skips_when_not_configured(monkeypatch, capsys):
    monkeypatch.delenv("SOCIAL_ENGINE_URL", raising=False)
    monkeypatch.delenv("SOCIAL_ENGINE_SECRET", raising=False)

    def _boom(*args, **kwargs):  # must never be called
        raise AssertionError("requests.post should not run when unconfigured")

    monkeypatch.setattr(studio_app.requests, "post", _boom)

    studio_app._queue_social_engine_post("alpha", "Alpha Store")
    assert "not configured" in capsys.readouterr().out


def test_queue_social_engine_post_sends_handle_and_name(monkeypatch):
    monkeypatch.setenv("SOCIAL_ENGINE_URL", "https://social.example.com/")
    monkeypatch.setenv("SOCIAL_ENGINE_SECRET", "s3cret")

    seen = {}

    class _Res:
        status_code = 200
        text = '{"queued": true}'

    def _fake_post(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _Res()

    monkeypatch.setattr(studio_app.requests, "post", _fake_post)

    studio_app._queue_social_engine_post("alpha", "Alpha Store")

    assert seen["url"] == "https://social.example.com/tasks/new-store"
    assert seen["params"] == {
        "key": "s3cret",
        "store_handle": "alpha",
        "store_name": "Alpha Store",
    }


def test_queue_social_engine_post_swallows_errors(monkeypatch, capsys):
    monkeypatch.setenv("SOCIAL_ENGINE_URL", "https://social.example.com")
    monkeypatch.setenv("SOCIAL_ENGINE_SECRET", "s3cret")

    def _fake_post(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(studio_app.requests, "post", _fake_post)

    # Must not raise — store-ready has already succeeded by this point.
    studio_app._queue_social_engine_post("alpha", "Alpha Store")
    assert "Failed to queue post" in capsys.readouterr().out
