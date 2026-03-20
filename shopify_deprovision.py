# shopify_deprovision.py — Shopify store deprovisioning (nuke) for Studio Uploader
# Reverse of shopify_provision.py — makes a store look like it never existed.
#
# Steps:
# 1. Look up custom_shop metaobject by handle via GraphQL Admin API
# 2. Extract logo file GIDs, collection GID from the metaobject
# 3. Remove store handle tag from all products in the collection (paginated)
# 4. Delete the Shopify collection
# 5. Strip storefront-admin--{handle} and storefront-member--{handle} tags from ALL
#    customers who have them (paginated, cursor-based GraphQL)
# 6. Delete the custom_shop metaobject entry
# 7. Delete logo files from Shopify Files API (if GIDs available)
#
# Usage as CLI:
#   python shopify_deprovision.py --handle <store-handle>
#
# Usage as callable:
#   from shopify_deprovision import deprovision
#   log = []
#   deprovision("my-store-handle", log)

from __future__ import annotations

import os
import json
import time
import argparse
from typing import Any, Dict, List, Optional

import requests


# -----------------------------
# ENV
# -----------------------------
def env_get(name: str, required: bool = True, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if required and (v is None or str(v).strip() == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return str(v).strip()


SHOP = env_get("SHOP", required=True)          # e.g. stellaandsage.myshopify.com
API_VERSION = env_get("API_VERSION", required=True)  # e.g. 2026-01
ACCESS_TOKEN = env_get("CLIENT_SECRET", required=True)  # Admin API access token

METAOBJECT_TYPE = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))


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
# Metaobject lookup
# -----------------------------
def get_metaobject_by_handle(handle: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the custom_shop metaobject for the given store handle.
    Returns the metaobject dict with id + fields, or None if not found.
    """
    q = """
    query getMetaobject($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
        handle
        type
        fields {
          key
          value
        }
      }
    }
    """
    data = shopify_graphql(q, {"handle": {"type": METAOBJECT_TYPE, "handle": handle}})
    return data.get("metaobjectByHandle")


def _metaobject_field(metaobject: Dict[str, Any], key: str) -> Optional[str]:
    """Extract a field value from the metaobject fields list."""
    for f in (metaobject.get("fields") or []):
        if f.get("key") == key:
            return f.get("value") or None
    return None


# -----------------------------
# Collection → products (paginated)
# -----------------------------
def get_collection_products(collection_id: str) -> List[Dict[str, Any]]:
    """
    Paginate through all products in a collection.
    Returns a list of dicts with {id, tags}.
    """
    q = """
    query getCollectionProducts($collectionId: ID!, $after: String) {
      collection(id: $collectionId) {
        products(first: 50, after: $after) {
          edges {
            node {
              id
              tags
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """
    products = []
    cursor = None

    while True:
        data = shopify_graphql(q, {"collectionId": collection_id, "after": cursor})
        col = data.get("collection")
        if not col:
            break
        page = col.get("products") or {}
        edges = page.get("edges") or []
        for edge in edges:
            node = edge.get("node")
            if node:
                products.append(node)

        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return products


# -----------------------------
# Product tag removal
# -----------------------------
def remove_tag_from_product(product_id: str, tag: str) -> None:
    """
    Remove a specific tag from a product (merge-safe — keeps all other tags).
    No-op if the product does not have the tag.
    """
    q_get = """
    query getProductTags($id: ID!) {
      product(id: $id) {
        id
        tags
      }
    }
    """
    data = shopify_graphql(q_get, {"id": product_id})
    product = data.get("product")
    if not product:
        return

    current_tags = product.get("tags") or []
    if tag not in current_tags:
        return

    new_tags = [t for t in current_tags if t != tag]

    q_update = """
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product { id tags }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql(q_update, {"input": {"id": product_id, "tags": new_tags}})
    errs = (res.get("productUpdate") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError(f"productUpdate userErrors: {json.dumps(errs, indent=2)}")


# -----------------------------
# Collection deletion
# -----------------------------
def delete_collection(collection_id: str) -> None:
    q = """
    mutation collectionDelete($input: CollectionDeleteInput!) {
      collectionDelete(input: $input) {
        deletedCollectionId
        userErrors { field message }
      }
    }
    """
    data = shopify_graphql(q, {"input": {"id": collection_id}})
    res = data.get("collectionDelete") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"collectionDelete userErrors: {json.dumps(errs, indent=2)}")


# -----------------------------
# Customer tag operations (paginated)
# -----------------------------
def get_customers_with_tag(tag: str) -> List[Dict[str, Any]]:
    """
    Return all customers who have the given tag.
    Uses cursor-based pagination.
    """
    q = """
    query getCustomersWithTag($query: String!, $after: String) {
      customers(first: 50, query: $query, after: $after) {
        edges {
          node {
            id
            tags
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
    """
    customers = []
    cursor = None
    search_query = f"tag:{tag}"

    while True:
        data = shopify_graphql(q, {"query": search_query, "after": cursor})
        page = data.get("customers") or {}
        edges = page.get("edges") or []
        for edge in edges:
            node = edge.get("node")
            if node:
                customers.append(node)

        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return customers


def customer_remove_tags(customer_gid: str, tags_to_remove: List[str]) -> None:
    """
    Remove specific tags from a customer (merge-safe — keeps all other tags).
    No-op if none of the tags are present.
    """
    q_get = """
    query getCustomer($id: ID!) {
      customer(id: $id) {
        id
        tags
      }
    }
    """
    data = shopify_graphql(q_get, {"id": customer_gid})
    cust = data.get("customer")
    if not cust:
        return

    existing = cust.get("tags") or []
    tags_to_remove_set = set(tags_to_remove)
    new_tags = [t for t in existing if t not in tags_to_remove_set]

    if len(new_tags) == len(existing):
        # Nothing to remove
        return

    q_update = """
    mutation customerUpdate($input: CustomerInput!) {
      customerUpdate(input: $input) {
        customer { id tags }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql(q_update, {"input": {"id": customer_gid, "tags": new_tags}})
    errs = (res.get("customerUpdate") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError(f"customerUpdate userErrors: {json.dumps(errs, indent=2)}")


# -----------------------------
# Metaobject deletion
# -----------------------------
def delete_metaobject(metaobject_id: str) -> None:
    q = """
    mutation metaobjectDelete($id: ID!) {
      metaobjectDelete(id: $id) {
        deletedId
        userErrors { field message }
      }
    }
    """
    data = shopify_graphql(q, {"id": metaobject_id})
    res = data.get("metaobjectDelete") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"metaobjectDelete userErrors: {json.dumps(errs, indent=2)}")


# -----------------------------
# File deletion
# -----------------------------
def delete_files(file_gids: List[str]) -> None:
    """Delete one or more files from Shopify Files (by GID)."""
    if not file_gids:
        return

    q = """
    mutation fileDelete($fileIds: [ID!]!) {
      fileDelete(fileIds: $fileIds) {
        deletedFileIds
        userErrors { field message }
      }
    }
    """
    data = shopify_graphql(q, {"fileIds": file_gids})
    res = data.get("fileDelete") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"fileDelete userErrors: {json.dumps(errs, indent=2)}")


# -----------------------------
# Main deprovision flow
# -----------------------------
def deprovision(handle: str, log: List[str]) -> None:
    """
    Nuke a store — make it look like it never existed.

    Args:
        handle: The store handle (e.g. "my-store")
        log:    A mutable list to append log messages to (also printed to stdout)
    """
    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    _log(f"💣 Starting deprovision for handle: {handle!r}")

    admin_tag = f"storefront-admin--{handle}"
    member_tag = f"storefront-member--{handle}"

    # ------------------------------------------------------------------
    # Step 1: Look up the custom_shop metaobject
    # ------------------------------------------------------------------
    _log(f"🔍 Step 1: Looking up metaobject {METAOBJECT_TYPE}/{handle}")
    metaobject = get_metaobject_by_handle(handle)
    if not metaobject:
        _log(f"⚠️  Metaobject not found for handle {handle!r} — it may already be deleted")
        metaobject_id = None
        collection_gid = None
        logo_gid = None
        secondary_logo_gid = None
    else:
        metaobject_id = metaobject["id"]
        _log(f"✅ Metaobject found: {metaobject_id}")

        # ------------------------------------------------------------------
        # Step 2: Extract GIDs from the metaobject
        # ------------------------------------------------------------------
        collection_gid = _metaobject_field(metaobject, "collection_gid")
        logo_gid = _metaobject_field(metaobject, "logo")
        secondary_logo_gid = _metaobject_field(metaobject, "secondary_logo")

        _log(f"📦 collection_gid: {collection_gid}")
        _log(f"🖼️  logo_gid: {logo_gid}")
        _log(f"🖼️  secondary_logo_gid: {secondary_logo_gid}")

    # ------------------------------------------------------------------
    # Step 3: Remove store handle tag from all products in the collection
    # ------------------------------------------------------------------
    if collection_gid:
        _log(f"📋 Step 3: Fetching products from collection {collection_gid}")
        try:
            products = get_collection_products(collection_gid)
            _log(f"   Found {len(products)} product(s) in collection")
            for product in products:
                pid = product["id"]
                if handle in (product.get("tags") or []):
                    _log(f"   Removing tag {handle!r} from product {pid}")
                    remove_tag_from_product(pid, handle)
                    _log(f"   ✅ Tag removed from product {pid}")
                else:
                    _log(f"   ℹ️  Product {pid} does not have tag {handle!r} — skipping")
        except Exception as e:
            _log(f"⚠️  Step 3 error (non-fatal): {e}")
    else:
        _log("ℹ️  Step 3: No collection_gid — skipping product tag removal")

    # ------------------------------------------------------------------
    # Step 4: Delete the collection
    # ------------------------------------------------------------------
    if collection_gid:
        _log(f"🗑️  Step 4: Deleting collection {collection_gid}")
        try:
            delete_collection(collection_gid)
            _log(f"✅ Collection deleted: {collection_gid}")
        except Exception as e:
            _log(f"⚠️  Step 4 error (non-fatal): {e}")
    else:
        _log("ℹ️  Step 4: No collection_gid — skipping collection deletion")

    # ------------------------------------------------------------------
    # Step 5: Strip storefront-admin/member tags from ALL customers
    # ------------------------------------------------------------------
    _log(f"👥 Step 5: Stripping tags {admin_tag!r} and {member_tag!r} from all customers")

    tags_to_strip = [admin_tag, member_tag]
    # Collect unique customer IDs from both tag searches
    tagged_customers: Dict[str, Dict[str, Any]] = {}
    for search_tag in tags_to_strip:
        _log(f"   Searching for customers with tag: {search_tag!r}")
        try:
            customers = get_customers_with_tag(search_tag)
            _log(f"   Found {len(customers)} customer(s) with tag {search_tag!r}")
            for c in customers:
                tagged_customers[c["id"]] = c
        except Exception as e:
            _log(f"⚠️  Step 5 tag search error ({search_tag!r}): {e}")

    _log(f"   Total unique customers to untag: {len(tagged_customers)}")
    for cid, cust in tagged_customers.items():
        try:
            customer_remove_tags(cid, tags_to_strip)
            _log(f"   ✅ Untagged customer {cid}")
        except Exception as e:
            _log(f"⚠️  Step 5 untag error for {cid}: {e}")

    # ------------------------------------------------------------------
    # Step 6: Delete the metaobject
    # ------------------------------------------------------------------
    if metaobject_id:
        _log(f"🗑️  Step 6: Deleting metaobject {metaobject_id}")
        try:
            delete_metaobject(metaobject_id)
            _log(f"✅ Metaobject deleted: {metaobject_id}")
        except Exception as e:
            _log(f"⚠️  Step 6 error (non-fatal): {e}")
    else:
        _log("ℹ️  Step 6: No metaobject_id — skipping")

    # ------------------------------------------------------------------
    # Step 7: Delete logo files
    # ------------------------------------------------------------------
    file_gids_to_delete = [g for g in [logo_gid, secondary_logo_gid] if g]
    if file_gids_to_delete:
        _log(f"🗑️  Step 7: Deleting {len(file_gids_to_delete)} logo file(s): {file_gids_to_delete}")
        try:
            delete_files(file_gids_to_delete)
            _log(f"✅ Logo files deleted")
        except Exception as e:
            _log(f"⚠️  Step 7 error (non-fatal): {e}")
    else:
        _log("ℹ️  Step 7: No logo files to delete")

    _log(f"🎉 Deprovision complete for handle: {handle!r}")


# -----------------------------
# CLI entry point
# -----------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Nuke a Shopify storefront — make it look like it never existed.")
    ap.add_argument("--handle", required=True, help="Store handle to deprovision (e.g. my-store)")
    args = ap.parse_args()

    handle = args.handle.strip()
    if not handle:
        raise SystemExit("--handle is required and cannot be empty")

    log: List[str] = []
    deprovision(handle, log)

    print("\n========== DEPROVISION LOG ==========")
    for line in log:
        print(line)


if __name__ == "__main__":
    main()
