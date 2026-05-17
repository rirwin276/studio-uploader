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


def _mock_metaobject_payload(*, is_fully_ready: str, status: str):
    return {
        "data": {
            "metaobjectByHandle": {
                "id": "gid://shopify/Metaobject/1",
                "handle": "alpha",
                "fields": [
                    {"key": "is_fully_ready", "value": is_fully_ready},
                    {"key": "status", "value": status},
                ],
            }
        }
    }


def test_ready_status_returns_handoff_signal_while_building(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "token")

    def _fake_post(*args, **kwargs):
        return _FakeResponse(_mock_metaobject_payload(is_fully_ready="false", status="building"))

    monkeypatch.setattr(studio_app.requests, "post", _fake_post)

    client = TestClient(studio_app.app)
    res = client.get("/store/alpha/ready-status")

    assert res.status_code == 200
    assert res.json() == {
        "ready": False,
        "is_fully_ready": False,
        "can_show_card": True,
        "status": "building",
        "handle": "alpha",
    }


def test_ready_status_keeps_ready_strict_but_reports_card_visibility(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("CLIENT_SECRET", "token")

    def _fake_post(*args, **kwargs):
        return _FakeResponse(_mock_metaobject_payload(is_fully_ready="true", status="active"))

    monkeypatch.setattr(studio_app.requests, "post", _fake_post)

    client = TestClient(studio_app.app)
    res = client.get("/store/alpha/ready-status")

    assert res.status_code == 200
    assert res.json() == {
        "ready": True,
        "is_fully_ready": True,
        "can_show_card": True,
        "status": "active",
        "handle": "alpha",
    }
