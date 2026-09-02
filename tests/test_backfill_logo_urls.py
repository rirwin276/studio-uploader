# -*- coding: utf-8 -*-
"""The backfill's logic, exercised against a fake Shopify.

This script writes to a live store and cannot be rehearsed there, so its
behaviour is pinned here instead: what it skips, what it fills, that --dry-run
writes nothing, and that it pages past the first fifty stores.

The rules that matter, because getting them wrong touches real customer data:

  - it fills only fields that are EMPTY, so running it twice is a no-op and it
    can never overwrite something fresher;
  - it never touches the logo reference itself — it copies that logo's URL into
    a text field and nothing else;
  - a store with no logo is left alone rather than written blank;
  - --dry-run performs no writes at all.
"""
from __future__ import annotations

import os

os.environ.setdefault("SHOP", "example.myshopify.com")
os.environ.setdefault("API_VERSION", "2026-01")
os.environ.setdefault("CLIENT_SECRET", "test-token")

import backfill_logo_urls as bf  # noqa: E402
import shopify_provision as sp  # noqa: E402


def store(handle, *, logo_url=None, text_url=None, node_id=None):
    fields = [{"key": "name", "value": handle}]
    fields.append({
        "key": "logo",
        "value": "gid://shopify/MediaImage/1" if logo_url else "",
        "reference": {"image": {"url": logo_url}} if logo_url else None,
    })
    if text_url is not None:
        fields.append({"key": "logo_url", "value": text_url})
    return {"id": node_id or ("gid://shopify/Metaobject/" + handle), "handle": handle, "fields": fields}


class FakeShopify:
    def __init__(self, pages, declared=("logo", "logo_url")):
        self.pages = pages
        self.declared = declared
        self.writes = []
        self._page = 0

    def __call__(self, query, variables=None):
        if "metaobjectDefinitionByType" in query:
            return {"metaobjectDefinitionByType": {
                "fieldDefinitions": [{"key": k} for k in self.declared]}}
        if "metaobjectUpdate" in query:
            self.writes.append(variables)
            return {"metaobjectUpdate": {"userErrors": []}}
        nodes = self.pages[self._page]
        has_next = self._page < len(self.pages) - 1
        self._page += 1
        return {"metaobjects": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": "c%d" % self._page},
            "nodes": nodes,
        }}


def run(monkeypatch, fake, argv):
    monkeypatch.setattr(sp, "shopify_graphql", fake)
    monkeypatch.setattr(bf.sp, "shopify_graphql", fake)
    sp._definition_field_keys_cache.clear()
    monkeypatch.setattr("sys.argv", ["backfill_logo_urls.py"] + argv)
    return bf.main()


def test_fills_only_the_empty_ones(monkeypatch):
    fake = FakeShopify([[
        store("needs-one", logo_url="https://cdn/a.png?v=1"),
        store("already-done", logo_url="https://cdn/b.png", text_url="https://cdn/b.png?width=512"),
        store("no-logo-at-all"),
    ]])
    assert run(monkeypatch, fake, []) == 0

    written = {w["fields"][0]["value"] for w in fake.writes}
    assert written == {"https://cdn/a.png?v=1&width=512"}
    assert len(fake.writes) == 1, "a store that already had a URL was written again"


def test_dry_run_writes_nothing(monkeypatch):
    fake = FakeShopify([[store("needs-one", logo_url="https://cdn/a.png")]])
    assert run(monkeypatch, fake, ["--dry-run"]) == 0
    assert fake.writes == []


def test_running_it_twice_changes_nothing(monkeypatch):
    """The second pass sees the field filled, so it must do nothing."""
    filled = store("done", logo_url="https://cdn/a.png", text_url="https://cdn/a.png?width=512")
    fake = FakeShopify([[filled]])
    assert run(monkeypatch, fake, []) == 0
    assert fake.writes == []


def test_pages_past_the_first_fifty(monkeypatch):
    fake = FakeShopify([
        [store("page1-%d" % i, logo_url="https://cdn/%d.png" % i) for i in range(50)],
        [store("page2-%d" % i, logo_url="https://cdn/x%d.png" % i) for i in range(6)],
    ])
    assert run(monkeypatch, fake, []) == 0
    assert len(fake.writes) == 56, "stores past the first page were never reached"


def test_stops_when_the_definition_has_no_field(monkeypatch):
    """Better to say so than to write to a key that does not exist."""
    fake = FakeShopify([[store("x", logo_url="https://cdn/a.png")]], declared=("name", "logo"))
    assert run(monkeypatch, fake, []) == 2
    assert fake.writes == []


def test_a_failed_write_is_reported_not_swallowed(monkeypatch):
    fake = FakeShopify([[store("x", logo_url="https://cdn/a.png")]])

    def failing(query, variables=None):
        if "metaobjectUpdate" in query:
            return {"metaobjectUpdate": {"userErrors": [{"message": "nope"}]}}
        return fake(query, variables)

    assert run(monkeypatch, failing, []) == 1
