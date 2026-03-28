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
    v = os.getenv(name, default)
    if required and (v is None or str(v).strip() == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return str(v).strip() if v is not None else ""


SHOP = _env("SHOP", required=True)
API_VERSION = _env("API_VERSION", required=True)
ACCESS_TOKEN = _env("CLIENT_SECRET", required=True)
METAOBJECT_TYPE = _env("METAOBJECT_TYPE", default="custom_shop")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))

PRINTFUL_AUTOMATION_URL = _env(
    "PRINTFUL_AUTOMATION_URL",
    default="https://printfulautomation-production.up.railway.app",
)


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


# -----------------------------
# Printful Automation trigger
# -----------------------------
def _trigger_printful_run(handle: str) -> Optional[str]:
    """
    POST to Printful Automation /run with the store handle.
    Returns the job_id string, or None if the response doesn't include one.
    """
    url = f"{PRINTFUL_AUTOMATION_URL.rstrip('/')}/run"
    r = requests.post(
        url,
        json={"store_handle": handle},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    return str(body.get("job_id") or body.get("id") or "") or None


# -----------------------------
# Main wakeup flow
# -----------------------------
def wakeup(handle: str, log: List[str]) -> Optional[str]:
    """
    Wake up a sleeping store.

    1. Reset metaobject status to "active" and clear slept_at.
    2. Trigger Printful Automation /run to re-create products.
    3. Return the Printful job_id (or None).

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
        # Reset status fields
        fields_to_update = [
            {"key": "status", "value": "active"},
            {"key": "slept_at", "value": ""},
        ]
        try:
            _update_metaobject_fields(mo_id, fields_to_update)
            _log(f"   ✅ Metaobject status reset to active")
        except Exception as e:
            _log(f"   ⚠️  Failed to update metaobject status in bulk: {e}")
            # Try each field individually
            for field in fields_to_update:
                try:
                    _update_metaobject_fields(mo_id, [field])
                    _log(f"   ✅ Updated field {field['key']}")
                except Exception as e2:
                    _log(f"   ⚠️  Could not update field {field['key']}: {e2}")

    # Trigger Printful Automation
    _log(f"   🚀 Triggering Printful Automation for {handle!r}")
    try:
        job_id = _trigger_printful_run(handle)
        _log(f"   ✅ Printful Automation triggered — job_id={job_id!r}")
        return job_id
    except Exception as e:
        _log(f"   ❌ Failed to trigger Printful Automation: {e}")
        raise


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
