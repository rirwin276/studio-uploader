# shopify_sleep.py — Sleep mode checker for Studio Uploader
#
# After SLEEP_INACTIVITY_DAYS days of no order activity a store enters sleep mode:
#   - All products tagged with the store handle are deleted
#   - The metaobject, collection, customer tags and logo files are NOT touched
#   - The metaobject's status field is set to "sleeping" and slept_at is recorded
#
# Usage as module:
#   from shopify_sleep import run_sleep_check
#   log = []
#   run_sleep_check(log)
#
# Usage as CLI:
#   python shopify_sleep.py

from __future__ import annotations

import os
import json
import datetime
from typing import Any, Dict, List, Optional

import requests


# -----------------------------
# ENV
# -----------------------------
def _env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    v = os.getenv(name, default)
    if required and (v is None or str(v).strip() == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return str(v).strip() if v is not None else ""


SHOP = _env("SHOP", required=True)
API_VERSION = _env("API_VERSION", required=True)
ACCESS_TOKEN = _env("CLIENT_SECRET", required=True)
METAOBJECT_TYPE = _env("METAOBJECT_TYPE", default="custom_shop")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
SLEEP_INACTIVITY_DAYS = int(os.getenv("SLEEP_INACTIVITY_DAYS", "28"))


# -----------------------------
# Shopify GraphQL
# -----------------------------
def _shopify_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN,
    }
    r = requests.post(
        url,
        headers=headers,
        json={"query": query, "variables": variables},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors:\n{json.dumps(payload['errors'], indent=2)}")
    data = payload.get("data")
    if data is None:
        raise RuntimeError(f"Shopify GraphQL returned no data:\n{json.dumps(payload, indent=2)}")
    return data


# -----------------------------
# Metaobject helpers
# -----------------------------
def _metaobject_field(metaobject: Dict[str, Any], key: str) -> Optional[str]:
    """Extract a field value from the metaobject fields list."""
    for f in (metaobject.get("fields") or []):
        if f.get("key") == key:
            return f.get("value") or None
    return None


def _get_all_metaobjects() -> List[Dict[str, Any]]:
    """
    Paginate through all custom_shop metaobjects and return them.
    Each item has: id, handle, fields list.
    """
    q = """
    query listMetaobjects($type: String!, $after: String) {
      metaobjects(type: $type, first: 50, after: $after) {
        edges {
          node {
            id
            handle
            fields {
              key
              value
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
    """
    results: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        data = _shopify_graphql(q, {"type": METAOBJECT_TYPE, "after": cursor})
        page = data.get("metaobjects") or {}
        for edge in (page.get("edges") or []):
            node = edge.get("node")
            if node:
                results.append(node)
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return results


def _update_metaobject_fields(metaobject_id: str, fields: List[Dict[str, str]]) -> None:
    """
    Update specific fields on a metaobject using metaobjectUpdate mutation.
    fields: list of {"key": "...", "value": "..."}
    """
    q = """
    mutation metaobjectUpdate($id: ID!, $metaobject: MetaobjectUpdateInput!) {
      metaobjectUpdate(id: $id, metaobject: $metaobject) {
        metaobject {
          id
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    data = _shopify_graphql(q, {"id": metaobject_id, "metaobject": {"fields": fields}})
    res = data.get("metaobjectUpdate") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"metaobjectUpdate userErrors: {json.dumps(errs, indent=2)}")


def _get_metaobject_id_by_handle(handle: str) -> Optional[str]:
    """Look up a metaobject by handle and return its GID."""
    q = """
    query getMetaobject($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        id
      }
    }
    """
    data = _shopify_graphql(q, {"handle": {"type": METAOBJECT_TYPE, "handle": handle}})
    mo = data.get("metaobjectByHandle")
    return mo["id"] if mo else None


# -----------------------------
# Order activity check (REST)
# -----------------------------
def _store_has_recent_orders(handle: str, days: int) -> bool:
    """
    Returns True if any Shopify order tagged with `handle` was created in the
    last `days` days.  Uses the REST Admin API (orders.json).
    """
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    url = (
        f"https://{SHOP}/admin/api/{API_VERSION}/orders.json"
        f"?tag={handle}&created_at_min={since}&status=any&limit=1&fields=id"
    )
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    orders = r.json().get("orders") or []
    return len(orders) > 0


# -----------------------------
# Product deletion (paginated)
# -----------------------------
def _get_products_by_tag(handle: str) -> List[Dict[str, Any]]:
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
    products: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        data = _shopify_graphql(q, {"query": f"tag:{handle}", "after": cursor})
        page = data.get("products") or {}
        for edge in (page.get("edges") or []):
            node = edge.get("node")
            if node:
                products.append(node)
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return products


def _delete_product(product_id: str) -> None:
    q = """
    mutation productDelete($input: ProductDeleteInput!) {
      productDelete(input: $input) {
        deletedProductId
        userErrors { field message }
      }
    }
    """
    data = _shopify_graphql(q, {"input": {"id": product_id}})
    res = data.get("productDelete") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"productDelete userErrors: {json.dumps(errs, indent=2)}")


# -----------------------------
# Sleep a single store
# -----------------------------
def sleep_store(handle: str, metaobject_id: str, log: List[str]) -> None:
    """
    Put one store to sleep:
    1. Delete all products tagged with handle
    2. Set status=sleeping and slept_at on the metaobject
    """
    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    _log(f"😴 Putting store {handle!r} to sleep")

    # --- Delete products ---
    try:
        products = _get_products_by_tag(handle)
        _log(f"   Found {len(products)} product(s) to delete")
        for product in products:
            pid = product["id"]
            if handle in (product.get("tags") or []):
                try:
                    _delete_product(pid)
                    _log(f"   ✅ Deleted product {pid}")
                except Exception as e:
                    _log(f"   ⚠️  Failed to delete product {pid}: {e}")
            else:
                _log(f"   ℹ️  Product {pid} missing tag {handle!r} — skipping")
    except Exception as e:
        _log(f"   ⚠️  Product deletion error (non-fatal): {e}")

    # --- Update metaobject status ---
    slept_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _update_metaobject_fields(metaobject_id, [
            {"key": "status", "value": "sleeping"},
            {"key": "slept_at", "value": slept_at},
        ])
        _log(f"   ✅ Metaobject status set to sleeping, slept_at={slept_at}")
    except Exception as e:
        _log(f"   ⚠️  Failed to update metaobject status+slept_at: {e}")
        # slept_at field may not exist in the metaobject definition — always fall back to status-only
        _log(f"   ↩️  Retrying with status-only update")
        try:
            _update_metaobject_fields(metaobject_id, [{"key": "status", "value": "sleeping"}])
            _log(f"   ✅ Metaobject status set to sleeping (slept_at skipped)")
        except Exception as e2:
            _log(f"   ⚠️  Could not update metaobject status: {e2}")


# -----------------------------
# Main sleep-check loop
# -----------------------------
def run_sleep_check(log: List[str]) -> None:
    """
    Check all active stores for inactivity. Put any store that has had no
    orders in SLEEP_INACTIVITY_DAYS days to sleep.
    """
    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    _log(f"🔍 Starting sleep check (inactivity threshold: {SLEEP_INACTIVITY_DAYS} days)")

    try:
        metaobjects = _get_all_metaobjects()
    except Exception as e:
        _log(f"❌ Failed to fetch metaobjects: {e}")
        return

    _log(f"   Found {len(metaobjects)} store(s) total")
    now = datetime.datetime.now(datetime.timezone.utc)
    threshold = datetime.timedelta(days=SLEEP_INACTIVITY_DAYS)

    for mo in metaobjects:
        handle = mo.get("handle") or ""
        mo_id = mo.get("id") or ""
        if not handle or not mo_id:
            continue

        # Skip already-sleeping stores
        status = _metaobject_field(mo, "status") or ""
        if status == "sleeping":
            _log(f"   ⏭️  {handle!r} already sleeping — skipping")
            continue

        _log(f"   Checking store {handle!r} (status={status!r})")

        # Check for recent orders
        try:
            has_orders = _store_has_recent_orders(handle, SLEEP_INACTIVITY_DAYS)
        except Exception as e:
            _log(f"   ⚠️  Order check failed for {handle!r}: {e} — skipping")
            continue

        if has_orders:
            # Update last_active on the metaobject
            _log(f"   ✅ {handle!r} has recent orders — updating last_active")
            today = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                _update_metaobject_fields(mo_id, [{"key": "last_active", "value": today}])
            except Exception as e:
                _log(f"   ⚠️  Could not update last_active for {handle!r}: {e}")
            continue

        # No recent orders — check last_active timestamp
        last_active_raw = _metaobject_field(mo, "last_active")
        if last_active_raw:
            try:
                # Parse ISO 8601 (Shopify stores as "YYYY-MM-DDTHH:MM:SSZ" or date-only)
                last_active_raw_clean = last_active_raw.rstrip("Z")
                last_active_dt = datetime.datetime.fromisoformat(last_active_raw_clean)
                inactive_for = now - last_active_dt
                if inactive_for < threshold:
                    _log(
                        f"   ⏳ {handle!r} inactive for {inactive_for.days}d — below threshold, skipping"
                    )
                    continue
                _log(f"   💤 {handle!r} inactive for {inactive_for.days}d — triggering sleep")
            except Exception as e:
                _log(
                    f"   ⚠️  Could not parse last_active for {handle!r}: {e!r} — treating as expired, sleeping"
                )
        else:
            # No last_active field — no orders found either; put to sleep
            _log(
                f"   💤 {handle!r} has no last_active and no recent orders — triggering sleep"
            )

        # Trigger sleep
        try:
            sleep_store(handle, mo_id, log)
        except Exception as e:
            _log(f"   ❌ Unexpected error sleeping {handle!r}: {e}")

    _log("✅ Sleep check complete")


# -----------------------------
# CLI entry point
# -----------------------------
if __name__ == "__main__":
    import sys

    _log_lines: List[str] = []
    run_sleep_check(_log_lines)
    print("\n========== SLEEP CHECK LOG ==========")
    for line in _log_lines:
        print(line)
    sys.exit(0)
