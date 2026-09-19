# shopify_deprovision.py — Shopify store deprovisioning (nuke) for Studio Uploader
# Reverse of shopify_provision.py — makes a store look like it never existed.
#
# Steps:
# 1. Look up custom_shop metaobject by handle via GraphQL Admin API
# 2. Extract logo file GIDs, collection GID from the metaobject
# 3. Inventory product/variant print-file metafields before deleting anything
# 4. Delete all products with the store handle tag, then their unique Shopify Files
# 5. Delete the Shopify collection
# 6. Strip storefront-admin--{handle} and storefront-member--{handle} tags from ALL
#    customers who have them (paginated, cursor-based GraphQL)
# 7. Delete logo files from Shopify Files API (if GIDs available)
# 8. Delete the custom_shop metaobject entry
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
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import unquote, urlsplit, urlunsplit

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
# Products by tag (paginated)
# -----------------------------
def get_products_by_tag(handle: str) -> List[Dict[str, Any]]:
    """
    Return all products that have the given store handle as a tag.
    Uses cursor-based pagination.
    """
    q = """
    query getProductsByTag($query: String!, $after: String) {
      products(first: 50, query: $query, after: $after) {
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
    products = []
    cursor = None
    search_query = f"tag:{handle}"

    while True:
        data = shopify_graphql(q, {"query": search_query, "after": cursor})
        page = data.get("products") or {}
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
# Product-owned Shopify Files
# -----------------------------
def _canonical_url(value: str) -> str:
    """Normalize a CDN URL for exact identity comparisons."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))


def _shopify_file_url(value: Any) -> str:
    """Return a canonical Shopify Files URL, never an arbitrary external URL."""
    url = _canonical_url(str(value or ""))
    if not url:
        return ""
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    if "/files/" not in parsed.path or not (
        host == "cdn.shopify.com" or host.endswith(".myshopify.com")
    ):
        return ""
    return url


def _urls_in_json(value: Any) -> Set[str]:
    found: Set[str] = set()
    if isinstance(value, dict):
        for nested in value.values():
            found.update(_urls_in_json(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_urls_in_json(nested))
    elif isinstance(value, str):
        url = _shopify_file_url(value)
        if url:
            found.add(url)
    return found


def print_file_urls_from_metafields(metafields: Iterable[Dict[str, Any]]) -> Set[str]:
    """Collect print assets from product and variant ``custom`` metafields.

    Today those are primarily product-level ``*_print_file_url`` values and
    variant-level ``print_map`` JSON. Walking every custom JSON value keeps the
    nuke path safe when new placements (sleeves, embroidery, etc.) are added.
    Only Shopify Files URLs are accepted; external/shared service URLs are not.
    """
    found: Set[str] = set()
    for metafield in metafields or []:
        if str(metafield.get("namespace") or "custom") != "custom":
            continue
        raw = metafield.get("value")
        direct = _shopify_file_url(raw)
        if direct:
            found.add(direct)
            continue
        if not isinstance(raw, str):
            continue
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        found.update(_urls_in_json(decoded))
    return found


def get_product_print_file_urls(product_id: str) -> Set[str]:
    """Read all product and variant metafields before the product is deleted."""
    q = """
    query getProductPrintFiles($id: ID!, $after: String) {
      product(id: $id) {
        metafields(first: 100, namespace: "custom") {
          nodes { namespace key type value }
        }
        variants(first: 100, after: $after) {
          nodes {
            metafields(first: 100, namespace: "custom") {
              nodes { namespace key type value }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    urls: Set[str] = set()
    cursor = None
    while True:
        data = shopify_graphql(q, {"id": product_id, "after": cursor})
        product = data.get("product")
        if not product:
            return urls
        urls.update(print_file_urls_from_metafields((product.get("metafields") or {}).get("nodes") or []))
        variants = product.get("variants") or {}
        for variant in variants.get("nodes") or []:
            urls.update(
                print_file_urls_from_metafields(
                    ((variant or {}).get("metafields") or {}).get("nodes") or []
                )
            )
        page = variants.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return urls
        cursor = page.get("endCursor")


def _file_node_urls(node: Dict[str, Any]) -> Set[str]:
    urls: Set[str] = set()
    direct = _shopify_file_url(node.get("url"))
    if direct:
        urls.add(direct)
    image = node.get("image") or {}
    image_url = _shopify_file_url(image.get("url"))
    if image_url:
        urls.add(image_url)
    return urls


def get_files(query: str) -> List[Dict[str, Any]]:
    """Search Shopify Files with complete cursor pagination."""
    q = """
    query getStoreFiles($query: String!, $after: String) {
      files(first: 100, query: $query, after: $after) {
        nodes {
          id
          ... on MediaImage { image { url } }
          ... on GenericFile { url }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    result: List[Dict[str, Any]] = []
    cursor = None
    while True:
        data = shopify_graphql(q, {"query": query, "after": cursor})
        page = data.get("files") or {}
        result.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return result
        cursor = info.get("endCursor")


def _url_filename(url: str) -> str:
    return unquote(urlsplit(str(url or "")).path.rsplit("/", 1)[-1]).lower()


def get_file_gids_for_urls(urls: Iterable[str]) -> Set[str]:
    """Resolve current Shopify File GIDs from the URLs stored in metafields."""
    wanted = {_canonical_url(url) for url in urls}
    wanted.discard("")
    by_filename: Dict[str, Set[str]] = {}
    for url in wanted:
        by_filename.setdefault(_url_filename(url), set()).add(url)

    found: Set[str] = set()
    for filename, expected_urls in by_filename.items():
        # Filename search avoids scanning the merchant's entire Files library.
        for node in get_files(f'filename:"{filename}"'):
            if _file_node_urls(node) & expected_urls and node.get("id"):
                found.add(str(node["id"]))
    return found


def _store_owned_filename(filename: str, handle: str) -> bool:
    """Recognize only names generated uniquely for one storefront."""
    filename = str(filename or "").lower()
    handle = str(handle or "").lower()
    return bool(handle) and (
        filename.startswith(f"print_{handle}_")
        or filename.startswith(f"mockup_{handle}_")
        or filename.startswith(f"{handle}__")
    )


def get_store_named_file_gids(handle: str) -> Set[str]:
    """Find retryable/orphaned generated assets even after products are gone."""
    prefixes = (
        f"print_{handle}_",
        f"mockup_{handle}_",
        f"{handle}__",
    )
    found: Set[str] = set()
    for prefix in prefixes:
        for node in get_files(f"filename:{prefix}*"):
            if not node.get("id"):
                continue
            if any(_store_owned_filename(_url_filename(url), handle) for url in _file_node_urls(node)):
                found.add(str(node["id"]))
    return found


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
# Product deletion
# -----------------------------
def delete_product(product_id: str) -> None:
    """Delete a product from Shopify by GID."""
    q = """
    mutation productDelete($input: ProductDeleteInput!) {
      productDelete(input: $input) {
        deletedProductId
        userErrors { field message }
      }
    }
    """
    data = shopify_graphql(q, {"input": {"id": product_id}})
    res = data.get("productDelete") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"productDelete userErrors: {json.dumps(errs, indent=2)}")


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
    q = """
    mutation fileDelete($fileIds: [ID!]!) {
      fileDelete(fileIds: $fileIds) {
        deletedFileIds
        userErrors { field message }
      }
    }
    """
    # Shopify input arrays are capped. Chunking also gives a precise failure
    # instead of turning a large store cleanup into one oversized mutation.
    unique = list(dict.fromkeys(str(gid) for gid in file_gids if gid))
    for offset in range(0, len(unique), 100):
        batch = unique[offset:offset + 100]
        data = shopify_graphql(q, {"fileIds": batch})
        res = data.get("fileDelete") or {}
        errs = res.get("userErrors") or []
        if errs:
            raise RuntimeError(f"fileDelete userErrors: {json.dumps(errs, indent=2)}")
        deleted = {str(gid) for gid in (res.get("deletedFileIds") or [])}
        missing = [gid for gid in batch if gid not in deleted]
        if missing:
            raise RuntimeError(f"fileDelete did not confirm deletion of: {missing}")


# -----------------------------
# Main deprovision flow
# -----------------------------
class DeprovisionIncomplete(RuntimeError):
    """Some part of the nuke did not happen. The store is still partly there."""


def deprovision(handle: str, log: List[str]) -> List[str]:
    """
    Nuke a store — make it look like it never existed.

    Every step is individually non-fatal so one failure cannot strand the
    rest, but the failures are collected and returned. A caller that treats
    "it ran" as "it worked" would record a store as deleted while it is still
    live — and nothing would ever revisit it, because the record says gone.

    Args:
        handle: The store handle (e.g. "my-store")
        log:    A mutable list to append log messages to (also printed to stdout)

    Returns:
        The list of failures. Empty means the store is really gone.
    """
    failures: List[str] = []

    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    def _failed(step: str, detail: Any) -> None:
        failures.append(f"{step}: {detail}")

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
    # Step 3: Inventory products and their separately stored print files.
    # This must finish before a product is deleted or its metafields disappear.
    # ------------------------------------------------------------------
    _log(f"🔍 Step 3: Inventorying products and generated files for {handle!r}")
    try:
        tagged_products = get_products_by_tag(handle)
        safe_products = [
            product for product in tagged_products
            if handle in (product.get("tags") or [])
        ]
        print_file_urls: Set[str] = set()
        for product in safe_products:
            print_file_urls.update(get_product_print_file_urls(product["id"]))
        product_file_gids = get_file_gids_for_urls(print_file_urls)
        # The filename scan is essential for retries: after a partial cleanup,
        # the product/metafield may already be gone while its file remains.
        product_file_gids.update(get_store_named_file_gids(handle))
        _log(
            f"   Found {len(safe_products)} product(s), "
            f"{len(print_file_urls)} referenced print URL(s), and "
            f"{len(product_file_gids)} generated Shopify file(s)"
        )
    except Exception as e:
        _log(f"❌ Step 3 inventory failed before deletion: {e}")
        _failed("inventory", e)
        return failures

    # ------------------------------------------------------------------
    # Step 4: Delete products, then their unique print/mockup files.
    # Do not remove files while any product survives: its Printful print_map
    # still needs those URLs to fulfill an order.
    # ------------------------------------------------------------------
    _log(f"🗑️  Step 4: Deleting {len(safe_products)} product(s)")
    product_delete_failed = False
    for product in safe_products:
        pid = product["id"]
        try:
            _log(f"   Deleting product {pid}")
            delete_product(pid)
            _log(f"   ✅ Product deleted: {pid}")
        except Exception as e:
            product_delete_failed = True
            _log(f"   ⚠️  Failed to delete product {pid}: {e}")
            _failed("product", f"{pid} {e}")

    if product_delete_failed:
        _log("❌ Skipping generated-file deletion because at least one product still exists")
        return failures

    if product_file_gids:
        try:
            delete_files(sorted(product_file_gids))
            _log(f"✅ Deleted {len(product_file_gids)} product print/mockup file(s)")
        except Exception as e:
            _log(f"⚠️  Product file deletion failed: {e}")
            _failed("product-files", e)
            # Preserve the collection/metaobject for a clean retry. The next
            # pass can recover files by their handle-specific filenames even
            # though the products and metafields are already gone.
            return failures
    else:
        _log("ℹ️  No separate product print/mockup files found")

    # ------------------------------------------------------------------
    # Step 5: Delete the collection
    # ------------------------------------------------------------------
    if collection_gid:
        _log(f"🗑️  Step 5: Deleting collection {collection_gid}")
        try:
            delete_collection(collection_gid)
            _log(f"✅ Collection deleted: {collection_gid}")
        except Exception as e:
            _log(f"⚠️  Step 5 error (non-fatal): {e}")
            _failed("collection", e)
            return failures
    else:
        _log("ℹ️  Step 5: No collection_gid — skipping collection deletion")

    # ------------------------------------------------------------------
    # Step 6: Strip storefront-admin/member tags from ALL customers
    # ------------------------------------------------------------------
    _log(f"👥 Step 6: Stripping tags {admin_tag!r} and {member_tag!r} from all customers")

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
            _log(f"⚠️  Step 6 tag search error ({search_tag!r}): {e}")
            _failed("customer-tag-search", f"{search_tag} {e}")

    _log(f"   Total unique customers to untag: {len(tagged_customers)}")
    for cid, cust in tagged_customers.items():
        try:
            customer_remove_tags(cid, tags_to_strip)
            _log(f"   ✅ Untagged customer {cid}")
        except Exception as e:
            _log(f"⚠️  Step 6 untag error for {cid}: {e}")
            _failed("customer-untag", f"{cid} {e}")

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
            _failed("logo-files", e)
    else:
        _log("ℹ️  Step 7: No logo files to delete")

    # Keep the metaobject until every referenced logo is gone. If fileDelete
    # fails, leaving the reference in place makes the next nuke retryable.
    if any(failure.startswith("logo-files:") for failure in failures):
        _log("❌ Keeping the custom_shop metaobject so logo deletion can retry")
        return failures

    # ------------------------------------------------------------------
    # Step 8: Delete the metaobject last
    # ------------------------------------------------------------------
    if metaobject_id:
        _log(f"🗑️  Step 8: Deleting metaobject {metaobject_id}")
        try:
            delete_metaobject(metaobject_id)
            _log(f"✅ Metaobject deleted: {metaobject_id}")
        except Exception as e:
            _log(f"⚠️  Step 8 error (non-fatal): {e}")
            _failed("metaobject", e)
    else:
        _log("ℹ️  Step 8: No metaobject_id — skipping")

    if failures:
        _log(f"❌ Deprovision INCOMPLETE for {handle!r} — {len(failures)} step(s) failed:")
        for failure in failures:
            _log(f"   • {failure}")
    else:
        _log(f"🎉 Deprovision complete for handle: {handle!r}")
    return failures


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
    failures = deprovision(handle, log)

    print("\n========== DEPROVISION LOG ==========")
    for line in log:
        print(line)

    # The caller decides what a store is by our exit code. Reporting success
    # after a partial nuke is how a live store gets recorded as deleted and
    # never looked at again.
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
