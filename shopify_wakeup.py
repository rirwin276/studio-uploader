# shopify_wakeup.py — Wake up a sleeping store for Studio Uploader
#
# Calls the Printful Automation service to re-create products for the store,
# then resets the metaobject status back to "active".
#
# Usage as module:
#   from shopify_wakeup import wakeup
#   log = []
#   job_id = wakeup("my-store", log)
#
# Usage as CLI:
#   python shopify_wakeup.py --handle <store-handle>

from __future__ import annotations

import os
import json
import argparse
from typing import Any, Dict, List, Optional

import requests


# -----------------------------
# ENV
# -----------------------------
def _env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    # Fall back to default when the env var is missing OR explicitly set to empty string.
    v = os.getenv(name)
    if not (v and v.strip()):
        v = default
    if required and not (v and str(v).strip()):
        raise RuntimeError(f"Missing required env var: {name}")
    return str(v).strip() if v is not None else ""


SHOP = _env("SHOP", required=True)
API_VERSION = _env("API_VERSION", required=True)
ACCESS_TOKEN = _env("CLIENT_SECRET", required=True)
METAOBJECT_TYPE = _env("METAOBJECT_TYPE", default="custom_shop")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
COLLECTION_TEMPLATE_SUFFIX = _env("COLLECTION_TEMPLATE_SUFFIX", default="private-store")

PRINTFUL_AUTOMATION_URL = _env(
    "PRINTFUL_AUTOMATION_URL",
    default="https://printfulautomation-production.up.railway.app",
)
PRINTFUL_AUTOMATION_TOKEN = _env("PRINTFUL_AUTOMATION_TOKEN", default="")
# Use a longer timeout for Printful Automation calls since they can be slow.
PRINTFUL_TIMEOUT = int(os.getenv("PRINTFUL_TIMEOUT", "120"))


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


def _update_metaobject_fields(metaobject_id: str, fields: List[Dict[str, str]]) -> None:
    """Update specific fields on a metaobject."""
    q = """
    mutation metaobjectUpdate($id: ID!, $metaobject: MetaobjectUpdateInput!) {
      metaobjectUpdate(id: $id, metaobject: $metaobject) {
        metaobject { id }
        userErrors { field message }
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


def _get_metaobject_name(handle: str) -> Optional[str]:
    """Return the 'name' field value from the store's metaobject, or None."""
    q = """
    query getMetaobjectFields($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) {
        fields { key value }
      }
    }
    """
    data = _shopify_graphql(q, {"handle": {"type": METAOBJECT_TYPE, "handle": handle}})
    mo = data.get("metaobjectByHandle")
    if not mo:
        return None
    for f in (mo.get("fields") or []):
        if f.get("key") == "name":
            return f.get("value") or None
    return None


# -----------------------------
# Smart collection helpers
# -----------------------------
def _collection_by_handle(handle: str) -> Optional[Dict[str, Any]]:
    """Return the collection dict for the given handle, or None if not found."""
    q = """
    query collectionByHandle($handle: String!) {
      collectionByHandle(handle: $handle) {
        id
        handle
        title
      }
    }
    """
    data = _shopify_graphql(q, {"handle": handle})
    return data.get("collectionByHandle")


def _get_all_publication_ids() -> List[str]:
    q = """
    query pubs($first: Int!) {
      publications(first: $first) {
        edges { node { id } }
      }
    }
    """
    data = _shopify_graphql(q, {"first": 50})
    edges = data.get("publications", {}).get("edges") or []
    return [e["node"]["id"] for e in edges if e.get("node", {}).get("id")]


def _publish_collection(collection_id: str) -> None:
    pubs = _get_all_publication_ids()
    if not pubs:
        return
    q = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }
    """
    data = _shopify_graphql(q, {"id": collection_id, "input": [{"publicationId": pid} for pid in pubs]})
    errs = (data.get("publishablePublish") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError(f"publishablePublish userErrors: {json.dumps(errs, indent=2)}")


def _ensure_smart_collection(handle: str, title: str) -> str:
    """
    Ensure a smart collection exists for the store.
    The collection matches products tagged with the store handle.
    Returns the collection GID.
    """
    existing = _collection_by_handle(handle)
    if existing:
        return existing["id"]

    # Create the collection with a TAG EQUALS handle rule.
    q = """
    mutation collectionCreate($input: CollectionInput!) {
      collectionCreate(input: $input) {
        collection { id handle title }
        userErrors { field message }
      }
    }
    """
    variables = {
        "input": {
            "title": title,
            "handle": handle,
            "templateSuffix": COLLECTION_TEMPLATE_SUFFIX,
            "ruleSet": {
                "appliedDisjunctively": False,
                "rules": [{"column": "TAG", "relation": "EQUALS", "condition": handle}],
            },
        }
    }
    data = _shopify_graphql(q, variables)
    res = data["collectionCreate"]
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"collectionCreate userErrors: {json.dumps(errs, indent=2)}")
    col = res.get("collection")
    if not col:
        raise RuntimeError("collectionCreate returned no collection")
    return col["id"]


# -----------------------------
# Printful Automation trigger
# -----------------------------
def _trigger_printful_run(handle: str, log_fn) -> Optional[str]:
    """
    POST to Printful Automation /run (with /trigger_automation fallback).
    Sends a bearer token if PRINTFUL_AUTOMATION_TOKEN is set.
    Returns the job_id string, or None if unavailable.
    Raises RuntimeError if all endpoints fail.
    """
    candidates = [
        f"{PRINTFUL_AUTOMATION_URL.rstrip('/')}/run",
        f"{PRINTFUL_AUTOMATION_URL.rstrip('/')}/api/run",
        f"{PRINTFUL_AUTOMATION_URL.rstrip('/')}/trigger_automation",
    ]
    headers = {"Content-Type": "application/json"}
    if PRINTFUL_AUTOMATION_TOKEN:
        headers["Authorization"] = f"Bearer {PRINTFUL_AUTOMATION_TOKEN}"
    payload = {"store_handle": handle}
    params = {"store_handle": handle}

    log_fn(f"   🌐 Printful Automation base URL: {PRINTFUL_AUTOMATION_URL!r}")
    log_fn(f"   📦 Payload: {payload}")
    log_fn(f"   🔑 Auth header present: {bool(PRINTFUL_AUTOMATION_TOKEN)}")

    for url in candidates:
        log_fn(f"   🔗 Trying: POST {url}")
        try:
            r = requests.post(
                url,
                json=payload,
                params=params,
                headers=headers,
                timeout=PRINTFUL_TIMEOUT,
            )
            if r.status_code == 404:
                log_fn(f"   ⚠️  404 at {url} — trying next endpoint…")
                continue
            if not (200 <= r.status_code < 300):
                raise RuntimeError(
                    f"Printful returned HTTP {r.status_code} for {url}: {r.text[:1000]}"
                )
            log_fn(f"   📬 Response: HTTP {r.status_code} — {r.text[:500]!r}")
            try:
                body = r.json()
            except ValueError:
                body = {}
            job_id = str(body.get("job_id") or body.get("id") or "") or None
            log_fn(f"   ✅ Printful Automation triggered via {url} — job_id={job_id!r}")
            return job_id
        except RuntimeError:
            raise
        except requests.exceptions.RequestException as e:
            log_fn(f"   ❌ Request failed for {url}: {e}")
            continue
    raise RuntimeError(
        f"No valid Printful Automation endpoint found for handle {handle!r}. "
        f"Tried: {', '.join(candidates)}."
    )


# -----------------------------
# Main wakeup flow
# -----------------------------
def wakeup(handle: str, log: List[str]) -> Optional[str]:
    """
    Wake up a sleeping store.

    1. Set metaobject status to "waking" (intermediate state).
    2. Ensure the smart collection exists (create if missing) and publish it.
    3. Trigger Printful Automation /run to re-create products.
    4. Status remains "waking" + is_fully_ready stays false until
       Printful Automation finishes and sets is_fully_ready=true + status=active.

    If Printful fails, the status is reset back to "sleeping" so the admin can retry.

    Args:
        handle: store handle string
        log:    mutable list to append log messages to

    Returns:
        Printful Automation job_id string, or None if unavailable.
    """
    def _log(msg: str) -> None:
        log.append(msg)
        print(msg)

    _log(f"⏰ Waking up store {handle!r}")

    # Look up metaobject ID
    try:
        mo_id = _get_metaobject_id_by_handle(handle)
    except Exception as e:
        _log(f"❌ Failed to look up metaobject for {handle!r}: {e}")
        raise

    if not mo_id:
        _log(f"⚠️  No metaobject found for {handle!r} — cannot update status")
    else:
        # Step 1: Set status to "waking" and is_fully_ready to false (intermediate state)
        try:
            _update_metaobject_fields(mo_id, [
                {"key": "status", "value": "waking"},
                {"key": "is_fully_ready", "value": "false"},
            ])
            _log(f"   ✅ Metaobject status set to waking, is_fully_ready set to false")
        except Exception as e:
            _log(f"   ⚠️  Failed to set metaobject status to waking: {e}")

    # Step 2: Ensure the smart collection exists for this store
    _log(f"   🗂️  Ensuring smart collection exists for {handle!r}")
    try:
        storefront_name = _get_metaobject_name(handle) or handle
        collection_id = _ensure_smart_collection(handle, storefront_name)
        _log(f"   ✅ Smart collection ready: {collection_id}")
        try:
            _publish_collection(collection_id)
            _log(f"   ✅ Collection published to all publications")
        except Exception as pub_err:
            _log(f"   ⚠️  Could not publish collection (non-fatal): {pub_err}")
    except Exception as e:
        _log(f"   ⚠️  Smart collection step failed (non-fatal): {e}")

    # Step 3: Trigger Printful Automation
    _log(f"   🚀 Triggering Printful Automation for {handle!r}")
    try:
        job_id = _trigger_printful_run(handle, _log)
        _log(f"   ✅ Printful Automation triggered — job_id={job_id!r}")
    except Exception as e:
        _log(f"   ❌ Failed to trigger Printful Automation: {e}")
        # Reset status back to sleeping so admin can retry
        if mo_id:
            try:
                _update_metaobject_fields(mo_id, [{"key": "status", "value": "sleeping"}])
                _log(f"   ↩️  Metaobject status reset back to sleeping (retry is possible)")
            except Exception as reset_err:
                _log(f"   ⚠️  Failed to reset metaobject status to sleeping after Printful error: {reset_err}")
        raise

    # Status remains "waking" and is_fully_ready remains false until
    # Printful Automation completes and calls _mark_store_ready() which
    # sets both status=active and is_fully_ready=true together.
    _log(f"   ⏳ Store is waking — status will stay 'waking' until Printful Automation completes")

    return job_id


# -----------------------------
# CLI entry point
# -----------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Wake up a sleeping Shopify storefront.")
    ap.add_argument("--handle", required=True, help="Store handle to wake up (e.g. my-store)")
    args = ap.parse_args()

    handle = args.handle.strip()
    if not handle:
        raise SystemExit("--handle is required and cannot be empty")

    log_lines: List[str] = []
    job_id = wakeup(handle, log_lines)

    print("\n========== WAKEUP LOG ==========")
    for line in log_lines:
        print(line)
    print(f"\nPrintful job_id: {job_id}")


if __name__ == "__main__":
    main()
