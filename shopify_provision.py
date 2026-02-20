# shopify_provision.py

import os
import re
import json
import requests
from typing import Any, Dict, Optional, Tuple

# ----------------------------
# Metaobject config
# ----------------------------
METAOBJECT_TYPE = "custom_shop"
FIELD_NAME = "name"
FIELD_LOGO = "logo"
FIELD_SECONDARY_LOGO = "secondary_logo"
FIELD_OWNER_CUSTOMER_ID = "owner_customer_id"
FIELD_COLLECTION_GID = "collection_gid"
FIELD_COLLECTION_HANDLE = "collection_handle"
FIELD_HANDLE = "handle"

COLLECTION_TEMPLATE_SUFFIX = "private-store"

# ----------------------------
# Env / Shopify helpers
# ----------------------------
def env_get(key: str, fallback_key: str = None, default: str = "") -> str:
    val = os.getenv(key) or (os.getenv(fallback_key) if fallback_key else None)
    return val or default

def shopify_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    shop = env_get("SHOP", "SHOPIFY_SHOP")
    api_version = env_get("API_VERSION", default="2024-01")
    token = env_get("CLIENT_SECRET", "SHOPIFY_TOKEN")
    
    if not shop or not token:
        raise RuntimeError("Missing Shopify credentials in Railway variables.")

    url = f"https://{shop}/admin/api/{api_version}/graphql.json"

    r = requests.post(
        url,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        json={"query": query, "variables": variables},
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors:\n{json.dumps(data['errors'], indent=2)}")

    return data["data"]

# ----------------------------
# Shopify Files upload
# ----------------------------
def upload_png_to_shopify_files(filename: str, png_bytes: bytes) -> Tuple[str, str]:
    q1 = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters { name value }
        }
        userErrors { field message }
      }
    }
    """
    variables1 = {"input": [{"resource": "FILE", "filename": filename, "mimeType": "image/png", "httpMethod": "POST"}]}
    d1 = shopify_graphql(q1, variables1)
    if d1["stagedUploadsCreate"]["userErrors"]:
        raise RuntimeError(f"stagedUploadsCreate errors: {d1['stagedUploadsCreate']['userErrors']}")

    target = d1["stagedUploadsCreate"]["stagedTargets"][0]
    upload_url = target["url"]
    params = {p["name"]: p["value"] for p in target["parameters"]}
    resource_url = target["resourceUrl"]

    files = {"file": (filename, png_bytes, "image/png")}
    r = requests.post(upload_url, data=params, files=files, timeout=120)
    r.raise_for_status()

    q2 = """
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          ... on MediaImage { id image { url } }
          ... on GenericFile { id url }
        }
        userErrors { field message }
      }
    }
    """
    d2 = shopify_graphql(q2, {"files": [{"originalSource": resource_url, "contentType": "IMAGE"}]})
    if d2["fileCreate"]["userErrors"]:
        raise RuntimeError(f"fileCreate errors: {d2['fileCreate']['userErrors']}")

    created = d2["fileCreate"]["files"][0]
    file_id = created["id"]
    file_url = created.get("image", {}).get("url") or created.get("url", "")

    return file_id, file_url

# ----------------------------
# Collection create/update
# ----------------------------
def ensure_collection(handle: str, title: str) -> Tuple[str, str]:
    q_find = """
    query getCollectionByHandle($handle: String!) {
      collectionByHandle(handle: $handle) { id handle title }
    }
    """
    existing = shopify_graphql(q_find, {"handle": handle}).get("collectionByHandle")

    if existing and existing.get("id"):
        q_upd = """
        mutation collectionUpdate($input: CollectionInput!) {
          collectionUpdate(input: $input) { collection { id handle } userErrors { field message } }
        }
        """
        d = shopify_graphql(q_upd, {"input": {"id": existing["id"], "title": title, "templateSuffix": COLLECTION_TEMPLATE_SUFFIX}})
        return d["collectionUpdate"]["collection"]["id"], handle

    q_create = """
    mutation collectionCreate($input: CollectionInput!) {
      collectionCreate(input: $input) { collection { id handle } userErrors { field message } }
    }
    """
    variables = {
        "input": {
            "title": title,
            "handle": handle,
            "ruleSet": {
                "appliedDisjunctively": False,
                "rules": [{"column": "TAG", "relation": "EQUALS", "condition": handle}]
            },
            "templateSuffix": COLLECTION_TEMPLATE_SUFFIX,
        }
    }
    d = shopify_graphql(q_create, variables)
    col = d["collectionCreate"]["collection"]
    return col["id"], col["handle"]

# ----------------------------
# Metaobject upsert
# ----------------------------
def upsert_custom_shop_metaobject(
    store_handle: str, store_name: str, owner_customer_id: str,
    collection_gid: str, collection_handle: str,
    main_logo_file_id: str, secondary_logo_file_id: Optional[str] = None,
) -> str:
    fields = [
        {"key": FIELD_NAME, "value": store_name},
        {"key": FIELD_OWNER_CUSTOMER_ID, "value": owner_customer_id},
        {"key": FIELD_COLLECTION_GID, "value": collection_gid},
        {"key": FIELD_COLLECTION_HANDLE, "value": collection_handle},
        {"key": FIELD_LOGO, "value": main_logo_file_id},
        {"key": FIELD_HANDLE, "value": store_handle}
    ]

    if secondary_logo_file_id:
        fields.append({"key": FIELD_SECONDARY_LOGO, "value": secondary_logo_file_id})

    q_upsert = """
    mutation metaobjectUpsert($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
        metaobject { id } userErrors { field message }
      }
    }
    """
    d = shopify_graphql(q_upsert, {"handle": {"type": METAOBJECT_TYPE, "handle": store_handle}, "metaobject": {"fields": fields}})
    return d["metaobjectUpsert"]["metaobject"]["id"]

# ----------------------------
# Trigger Automation
# ----------------------------
def trigger_studio_automation(handle: str, logo_url: str, store_name: str) -> None:
    url = env_get("STUDIO_AUTOMATION_URL", default="https://studio-automation-production.up.railway.app/trigger-automation")
    print(f"🚀 Triggering orchestrator for {handle}...")
    try:
        requests.post(url, json={"store_handle": handle, "logo_url": logo_url, "unit_name": store_name}, timeout=10)
    except Exception as e:
        print(f"⚠️ Orchestrator trigger warning: {e}")

# ----------------------------
# MAIN EXECUTION (Called by app.py)
# ----------------------------
def run_provisioning(store_name: str, handle: str, customer_id: str, main_png: bytes, sec_png: bytes = None):
    print(f"\n=== PROVISION START: {handle} ===")
    try:
        print("1) Uploading MAIN logo to Shopify Files…")
        main_file_id, main_url = upload_png_to_shopify_files(f"{handle}_logo.png", main_png)
        print(f"✅ Main file id: {main_file_id}")

        sec_file_id = None
        if sec_png:
            print("1b) Uploading SECONDARY logo…")
            sec_file_id, _ = upload_png_to_shopify_files(f"{handle}_secondary.png", sec_png)

        print("2) Ensuring collection exists…")
        collection_gid, collection_handle = ensure_collection(handle=handle, title=f"{store_name} Storefront")
        print(f"✅ Collection gid: {collection_gid}")

        print("3) Upserting custom_shop metaobject…")
        meta_id = upsert_custom_shop_metaobject(
            store_handle=handle, store_name=store_name, owner_customer_id=customer_id,
            collection_gid=collection_gid, collection_handle=collection_handle,
            main_logo_file_id=main_file_id, secondary_logo_file_id=sec_file_id,
        )
        print(f"✅ Metaobject id: {meta_id}")

        print("4) Triggering studio automation…")
        trigger_studio_automation(handle, main_url, store_name)

        print("=== PROVISION COMPLETE ===\n")
    except Exception as e:
        print(f"❌ PROVISIONING FAILED: {str(e)}")