# -*- coding: utf-8 -*-
"""The logo's URL is stored as text, so the dashboard need not resolve it.

Shopify resolves at most twenty metaobject FILE references per request. The
seller dashboard renders one card per store, and each card that resolves its
`logo` reference spends one of those twenty — so a seller with more than twenty
stores gets logos on the first twenty and empty frames on the rest. Measured on
a real account: cards 1-20 drew a logo, cards 21-36 drew nothing, with the break
falling between two alphabetically adjacent stores.

Reading a TEXT field costs nothing against that budget, and the CDN URL is
already in hand when the logo is uploaded — it was simply being discarded. So it
is written alongside the file reference.

The risk this guards is the write itself: metaobjectUpsert fails entirely on an
unknown field key, and this is the call that creates a paying customer's store.
A logo on a dashboard is not worth risking a provision over, so the key is
chosen from the definition and skipped when there is none.
"""
from __future__ import annotations

import os

# shopify_provision reads its Shopify credentials at import time, so these have
# to exist before the import rather than in a fixture. Nothing here talks to
# Shopify — shopify_graphql is replaced in every test.
os.environ.setdefault("SHOP", "example.myshopify.com")
os.environ.setdefault("API_VERSION", "2026-01")
os.environ.setdefault("CLIENT_SECRET", "test-token")

import shopify_provision as sp  # noqa: E402


def _capture(monkeypatch, declared_keys, fail_definition_query=False):
    """Run the upsert against a fake Shopify and return the fields it sent."""
    sent = {}

    def fake_graphql(query, variables=None):
        if "metaobjectDefinitionByType" in query:
            if fail_definition_query:
                raise RuntimeError("definition query exploded")
            return {
                "metaobjectDefinitionByType": {
                    "fieldDefinitions": [{"key": k} for k in declared_keys]
                }
            }
        sent["variables"] = variables
        return {
            "metaobjectUpsert": {
                "metaobject": {"id": "gid://shopify/Metaobject/1"},
                "userErrors": [],
            }
        }

    monkeypatch.setattr(sp, "shopify_graphql", fake_graphql)
    sp._definition_field_keys_cache.clear()
    return sent


def _upsert(url="https://cdn.example/logo.png"):
    return sp.metaobject_upsert_custom_shop(
        handle="raptors-3978",
        name="Raptors",
        owner_customer_id_text="123",
        collection_gid="gid://shopify/Collection/1",
        collection_handle="raptors-3978",
        logo_file_gid="gid://shopify/MediaImage/1",
        secondary_logo_file_gid=None,
        type_of_store="team",
        is_fully_ready=True,
        primary_color="Navy",
        logo_file_url=url,
    )


def _fields(sent):
    return {f["key"]: f["value"] for f in sent["variables"]["metaobject"]["fields"]}


def test_url_is_written_to_the_definitions_own_field(monkeypatch):
    sent = _capture(monkeypatch, ["name", "logo", "logo_url", "status"])
    _upsert()
    fields = _fields(sent)
    assert fields["logo_url"] == "https://cdn.example/logo.png?width=512"
    # The reference is still written: it is the source of truth, and the text
    # copy only exists so the dashboard does not have to resolve it.
    assert fields["logo"] == "gid://shopify/MediaImage/1"


def test_falls_back_to_the_older_clean_url_field(monkeypatch):
    sent = _capture(monkeypatch, ["name", "logo", "logo_clean_url"])
    _upsert()
    assert _fields(sent)["logo_clean_url"] == "https://cdn.example/logo.png?width=512"


def test_prefers_logo_url_when_both_exist(monkeypatch):
    sent = _capture(monkeypatch, ["logo", "logo_url", "logo_clean_url"])
    _upsert()
    fields = _fields(sent)
    assert fields.get("logo_url") == "https://cdn.example/logo.png?width=512"
    # logo_clean_url is the older background-removed copy and means something
    # different; writing the plain logo there when a proper field exists would
    # quietly redefine it.
    assert "logo_clean_url" not in fields


def test_a_definition_with_no_text_field_still_provisions(monkeypatch):
    """The whole point of asking first: an unknown key fails the upsert, and
    this call is what creates a customer's store."""
    sent = _capture(monkeypatch, ["name", "logo", "status"])
    result = _upsert()
    fields = _fields(sent)
    assert result == "gid://shopify/Metaobject/1"
    assert not any(k in fields for k in ("logo_url", "logo_clean_url"))
    assert fields["logo"] == "gid://shopify/MediaImage/1"


def test_a_failing_definition_query_still_provisions(monkeypatch):
    """Shopify being briefly unhappy about a nice-to-have must not cost a
    customer their store."""
    sent = _capture(monkeypatch, ["logo_url"], fail_definition_query=True)
    result = _upsert()
    assert result == "gid://shopify/Metaobject/1"
    assert "logo_url" not in _fields(sent)


def test_no_url_writes_no_text_field(monkeypatch):
    sent = _capture(monkeypatch, ["logo", "logo_url"])
    _upsert(url="")
    assert "logo_url" not in _fields(sent)


def test_the_stored_url_is_sized_for_the_card(monkeypatch):
    """A raw CDN URL goes straight into an <img> at 92px, so every card would
    pull a full-resolution logo. 512 is what the theme asked for when it still
    resolved the reference."""
    assert sp.logo_url_for_text("https://cdn/x.png") == "https://cdn/x.png?width=512"
    # Shopify's own URLs already carry a version parameter.
    assert sp.logo_url_for_text("https://cdn/x.png?v=1") == "https://cdn/x.png?v=1&width=512"
    # Never twice.
    assert sp.logo_url_for_text("https://cdn/x.png?width=512") == "https://cdn/x.png?width=512"
    assert sp.logo_url_for_text("") == ""
