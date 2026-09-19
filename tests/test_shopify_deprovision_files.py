from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def deprovision(monkeypatch):
    monkeypatch.setenv("SHOP", "example.myshopify.com")
    monkeypatch.setenv("API_VERSION", "2026-01")
    monkeypatch.setenv("CLIENT_SECRET", "test-token")
    sys.modules.pop("shopify_deprovision", None)
    return importlib.import_module("shopify_deprovision")


def test_extracts_product_and_variant_print_file_urls(deprovision):
    front = "https://cdn.shopify.com/s/files/1/0000/files/print_team_front.png?v=1"
    back = "https://cdn.shopify.com/s/files/1/0000/files/print_team_back.png?v=2"
    external = "https://files.printful.com/shared.png"
    metafields = [
        {"namespace": "custom", "key": "front_print_file_url", "value": front},
        {
            "namespace": "custom",
            "key": "print_map",
            "value": (
                '{"placements":{"back":{"url":"' + back + '"},'
                '"external":{"url":"' + external + '"}}}'
            ),
        },
    ]

    assert deprovision.print_file_urls_from_metafields(metafields) == {
        front.split("?", 1)[0],
        back.split("?", 1)[0],
    }


def test_reads_every_variant_page_before_product_deletion(deprovision, monkeypatch):
    calls = []

    def graphql(_query, variables):
        calls.append(variables["after"])
        suffix = "front" if variables["after"] is None else "back"
        return {
            "product": {
                "metafields": {"nodes": []},
                "variants": {
                    "nodes": [{
                        "metafields": {"nodes": [{
                            "namespace": "custom",
                            "key": "print_map",
                            "value": (
                                '{"placements":{"' + suffix + '":{"url":'
                                '"https://cdn.shopify.com/s/files/1/files/print_team_' + suffix + '.png"}}}'
                            ),
                        }]}
                    }],
                    "pageInfo": {
                        "hasNextPage": variables["after"] is None,
                        "endCursor": "page-2",
                    },
                },
            }
        }

    monkeypatch.setattr(deprovision, "shopify_graphql", graphql)
    assert deprovision.get_product_print_file_urls("gid://shopify/Product/1") == {
        "https://cdn.shopify.com/s/files/1/files/print_team_front.png",
        "https://cdn.shopify.com/s/files/1/files/print_team_back.png",
    }
    assert calls == [None, "page-2"]


def _wire_store(monkeypatch, module):
    events = []
    monkeypatch.setattr(module, "get_metaobject_by_handle", lambda _handle: {
        "id": "gid://shopify/Metaobject/1",
        "fields": [
            {"key": "collection_gid", "value": "gid://shopify/Collection/1"},
            {"key": "logo", "value": "gid://shopify/MediaImage/logo"},
        ],
    })
    monkeypatch.setattr(module, "get_products_by_tag", lambda _handle: [{
        "id": "gid://shopify/Product/1", "tags": ["team-demo"]
    }])
    monkeypatch.setattr(
        module,
        "get_product_print_file_urls",
        lambda _product: {"https://cdn.shopify.com/s/files/1/files/print_team-demo_front.png"},
    )
    monkeypatch.setattr(module, "get_file_gids_for_urls", lambda _urls: {"gid://shopify/MediaImage/print"})
    monkeypatch.setattr(module, "get_store_named_file_gids", lambda _handle: set())
    monkeypatch.setattr(module, "delete_product", lambda product: events.append(("product", product)))
    monkeypatch.setattr(module, "delete_collection", lambda collection: events.append(("collection", collection)))
    monkeypatch.setattr(module, "get_customers_with_tag", lambda _tag: [])
    monkeypatch.setattr(module, "delete_metaobject", lambda metaobject: events.append(("metaobject", metaobject)))
    return events


def test_nuke_deletes_print_files_before_collection_and_metaobject(deprovision, monkeypatch):
    events = _wire_store(monkeypatch, deprovision)
    monkeypatch.setattr(
        deprovision,
        "delete_files",
        lambda gids: events.append(("files", tuple(gids))),
    )

    assert deprovision.deprovision("team-demo", []) == []
    assert events == [
        ("product", "gid://shopify/Product/1"),
        ("files", ("gid://shopify/MediaImage/print",)),
        ("collection", "gid://shopify/Collection/1"),
        ("files", ("gid://shopify/MediaImage/logo",)),
        ("metaobject", "gid://shopify/Metaobject/1"),
    ]


def test_failed_print_file_delete_keeps_store_metadata_for_retry(deprovision, monkeypatch):
    events = _wire_store(monkeypatch, deprovision)

    def fail_files(gids):
        events.append(("files", tuple(gids)))
        raise RuntimeError("temporary Shopify error")

    monkeypatch.setattr(deprovision, "delete_files", fail_files)
    failures = deprovision.deprovision("team-demo", [])

    assert failures == ["product-files: temporary Shopify error"]
    assert events == [
        ("product", "gid://shopify/Product/1"),
        ("files", ("gid://shopify/MediaImage/print",)),
    ]


def test_file_delete_requires_confirmation_for_every_gid(deprovision, monkeypatch):
    monkeypatch.setattr(
        deprovision,
        "shopify_graphql",
        lambda _query, _variables: {
            "fileDelete": {
                "deletedFileIds": ["gid://shopify/MediaImage/1"],
                "userErrors": [],
            }
        },
    )
    with pytest.raises(RuntimeError, match="did not confirm"):
        deprovision.delete_files([
            "gid://shopify/MediaImage/1",
            "gid://shopify/MediaImage/2",
        ])
