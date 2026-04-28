# studio_utils/placement_overrides.py — Placement override helpers for Studio Uploader
# Manages Shopify metaobjects of type `placement_override`, which store custom logo
# positioning coordinates tuned via the placement editor.
#
# Metaobject handle format: `{product_handle}-{logo_kind}` slugified
# The `product_handle` field for a store's products follows: `{store_handle}-{product_slug}`

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import requests

# -----------------------------
# ENV
# -----------------------------
SHOP = os.getenv("SHOP", "").strip()
API_VERSION = os.getenv("API_VERSION", "2026-01").strip()
ACCESS_TOKEN = os.getenv("CLIENT_SECRET", "").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))

PLACEMENT_OVERRIDE_TYPE = "placement_override"


# -----------------------------
# Shopify GraphQL
# -----------------------------
def shopify_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN,
    }
    r = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    payload = r.json()

    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors:\n{json.dumps(payload['errors'], indent=2)}")

    data = payload.get("data")
    if data is None:
        raise RuntimeError(f"Shopify GraphQL returned no data:\n{json.dumps(payload, indent=2)}")

    return data


# -----------------------------
# Helpers
# -----------------------------
def _get_field(fields: list, key: str) -> Optional[str]:
    """Extract a field value from the metaobject fields list."""
    for f in fields or []:
        if f.get("key") == key:
            return f.get("value") or None
    return None


# -----------------------------
# Public API
# -----------------------------
def get_placement_override(product_handle: str, logo_kind: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a placement_override metaobject by product_handle + logo_kind.
    Returns a dict of field key→value, or None if not found.
    """
    handle = f"{product_handle}-{logo_kind}"
    q = """
    query GetOverride($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
        handle
        fields { key value }
      }
    }
    """
    data = shopify_graphql(q, {"handle": {"type": PLACEMENT_OVERRIDE_TYPE, "handle": handle}})
    mo = data.get("metaobjectByHandle")
    if not mo:
        return None
    return {f["key"]: f["value"] for f in (mo.get("fields") or [])}


def upsert_placement_override(
    product_handle: str,
    logo_kind: str,
    fields: Dict[str, str],
) -> str:
    """
    Create or update a placement_override metaobject.
    Returns the metaobject GID.
    """
    handle = f"{product_handle}-{logo_kind}"
    q = """
    mutation UpsertOverride($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
        metaobject { id }
        userErrors { field message }
      }
    }
    """
    gql_fields = [{"key": k, "value": v} for k, v in fields.items()]
    data = shopify_graphql(
        q,
        {
            "handle": {"type": PLACEMENT_OVERRIDE_TYPE, "handle": handle},
            "metaobject": {"fields": gql_fields},
        },
    )
    res = data.get("metaobjectUpsert") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"metaobjectUpsert userErrors: {json.dumps(errs, indent=2)}")
    mo = res.get("metaobject")
    if not mo:
        raise RuntimeError("metaobjectUpsert returned no metaobject")
    return mo["id"]


def delete_placement_overrides_for_store(store_handle: str) -> int:
    """
    Delete all placement_override metaobjects whose product_handle field
    starts with store_handle. Returns the count of deleted overrides.
    """
    list_q = """
    query ListOverrides($type: String!, $after: String) {
      metaobjects(type: $type, first: 50, after: $after) {
        edges {
          node {
            id
            handle
            fields { key value }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    delete_q = """
    mutation DeleteOverride($id: ID!) {
      metaobjectDelete(id: $id) {
        deletedId
        userErrors { field message }
      }
    }
    """

    prefix = f"{store_handle}-"
    to_delete = []

    cursor = None
    while True:
        variables: Dict[str, Any] = {"type": PLACEMENT_OVERRIDE_TYPE}
        if cursor:
            variables["after"] = cursor

        data = shopify_graphql(list_q, variables)
        result = data.get("metaobjects") or {}
        edges = result.get("edges") or []

        for edge in edges:
            node = edge.get("node") or {}
            product_handle = _get_field(node.get("fields") or [], "product_handle")
            if product_handle and (
                product_handle.startswith(prefix) or product_handle == store_handle
            ):
                to_delete.append((node["id"], node.get("handle", "")))

        page_info = result.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    deleted = 0
    for gid, handle in to_delete:
        data = shopify_graphql(delete_q, {"id": gid})
        res = (data.get("metaobjectDelete") or {})
        errs = res.get("userErrors") or []
        if errs:
            print(f"⚠️  metaobjectDelete userErrors for {handle!r}: {json.dumps(errs)}")
        else:
            print(f"🗑️  Deleted placement override: {handle!r} ({gid})")
            deleted += 1

    return deleted
