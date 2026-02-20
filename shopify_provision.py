# shopify_provision.py

import os
import re
import json
import requests
from typing import Any, Dict, Optional, List, Tuple

# ----------------------------
# Metaobject config
# ----------------------------
METAOBJECT_TYPE = "custom_shop"
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

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"['’]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "store"

def build_handle(store_name: str, owner_customer_id: str) -> str:
    last4 = re.sub(r"\D+", "", str(owner_customer_id))[-4:] or "0000"
    return f"{slugify(store_name)}-{last4}"

def customer_gid(customer_id: str) -> str:
    numeric = re.sub(r"\D+", "", str(customer_id))
    return f"gid://shopify/Customer/{numeric}"

# -----------------------------
# SHOPIFY: SAFE CUSTOMER TAGGING
# -----------------------------
def get_customer_tags(customer_id: str) -> List[str]:
    q = """
    query($id: ID!) {
      customer(id: $id) { id tags }
    }
    """
    gid = customer_gid(customer_id)
    data = shopify_graphql(q, {"id": gid})
    cust = data.get("customer")
    if not cust:
        raise RuntimeError(f"Customer not found for id: {customer_id}")
    return cust.get("tags") or []

def set_customer_tags(customer_id: str, tags: List[str]) -> None:
    q = """
    mutation customerUpdate($input: CustomerInput!) {
      customerUpdate(input: $input) {
        customer { id tags }
        userErrors { field message }
      }
    }
    """
    gid = customer_gid(customer_id)
    res = shopify_graphql(q, {"input": {"id": gid, "tags": tags}})["customerUpdate"]
    if res.get("userErrors"):
        raise RuntimeError(f"customerUpdate userErrors: {res['userErrors']}")

def ensure_customer_storefront_tags(owner_customer_id: str, store_handle: str) -> None:
    required = {
        f"storefront-member--{store_handle}",
        f"storefront-admin--{store_handle}",
    }
    current = set(get_customer_tags(owner_customer_id))
    merged = sorted(current.union(required))

    if merged == sorted(current):
        print("   ℹ️ Customer already has required tags.")
        return

    set_customer_tags(owner_customer_id, merged)
    print(f"   ✅ Customer securely tagged with {store_handle} tags.")

# ----------------------------
# Shopify Files upload
# ----------------------------
def upload_png_to_shopify_files(filename: str, png_bytes: bytes) -> Tuple[str, str]:
    q1 = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets { url resourceUrl parameters { name value } }
        userErrors { field message }
      }
    }
    """
    variables1 = {"input": [{"resource": "FILE", "filename": filename, "mimeType": "image/png", "httpMethod": "POST"}]}
    d1 = shopify_graphql(q1, variables1)
    target = d1["stagedUploadsCreate"]["stagedTargets"][0]
    
    upload_url = target["url"]
    params = {p["name"]: p["value"] for p in target["parameters"]}
    resource_url = target["resourceUrl"]

    files = {"file": (filename, png_bytes, "image/png")}
    requests.post(upload_url, data=params, files=files, timeout=120).raise_for_status()

    q2 = """
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files { ... on MediaImage { id image { url } } ... on GenericFile { id url } }
        userErrors { field message }
      }
    }
    """
    d2 = shopify_graphql(q2, {"files": [{"originalSource": resource_url, "contentType": "IMAGE"}]})
    created = d2["fileCreate"]["files"][0]
    return created["id"], created.get("image", {}).get("url") or created.get("url", "")

# ----------------------------
# Collection create/update
# ----------------------------
def ensure_smart_collection(handle: str, title: str) -> str:
    q_find = """
    query getCollectionByHandle($handle: String!) {
      collectionByHandle(handle: $handle) { id handle title }
    }
    """
    existing = shopify_graphql(q_find, {"handle": handle}).get("collectionByHandle")

    if existing and existing.get("id"):
        q_upd = """
        mutation collectionUpdate($input: CollectionInput!) {
          collectionUpdate(input: $input) { collection { id } userErrors { field message } }
        }
        """
        d = shopify_graphql(q_upd, {"input": {"id": existing["id"], "title": title, "templateSuffix": COLLECTION_TEMPLATE_SUFFIX}})
        return d["collectionUpdate"]["collection"]["id"]

    q_create = """
    mutation collectionCreate($input: CollectionInput!) {
      collectionCreate(input: $input) { collection { id } userErrors { field message } }
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
    return d["collectionCreate"]["collection"]["id"]

def publish_collection_to_online_store(collection_gid: str) -> Optional[str]:
    pub_id = env_get("ONLINE_STORE_PUBLICATION_ID")
    if not pub_id:
        return "ONLINE_STORE_PUBLICATION_ID not set in Railway — collection created but hidden."

    q = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) { userErrors { field message } }
    }
    """
    resp = shopify_graphql(q, {"id": collection_gid, "input": [{"publicationId": pub_id}]})
    return None

# ----------------------------
# Metaobject upsert
# ----------------------------
def upsert_custom_shop_metaobject(
    store_handle: str, store_name: str, owner_customer_id: str,
    collection_gid: str, collection_handle: str,
    main_logo_file_id: str, secondary_logo_file_id: Optional[str] = None, type_of_store: str = None
) -> str:
    fields = [
        {"key": "name", "value": store_name},
        {"key": "owner_customer_id", "value": str(owner_customer_id)},
        {"key": "collection_gid", "value": collection_gid},
        {"key": "collection_handle", "value": collection_handle},
        {"key": "logo", "value": main_logo_file_id},
        {"key": "handle", "value": store_handle}
    ]

    if secondary_logo_file_id:
        fields.append({"key": "secondary_logo", "value": secondary_logo_file_id})
    if type_of_store:
        fields.append({"key": "type_of_store", "value": type_of_store})

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
    url = env_get("STUDIO_AUTOMATION_URL")
    if not url:
        return
    try:
        requests.post(url, json={"store_handle": handle, "logo_url": logo_url, "unit_name": store_name}, timeout=10)
    except Exception:
        pass

# ----------------------------
# MAIN BACKGROUND TASK
# ----------------------------
def run_provisioning(store_name: str, customer_id: str, main_png: bytes, sec_png: bytes = None, type_of_store: str = None):
    handle = build_handle(store_name, customer_id)
    print(f"\n=== PROVISION START: {handle} ===")
    
    try:
        print("1) Uploading MAIN logo to Shopify Files…")
        main_file_id, main_url = upload_png_to_shopify_files(f"{handle}_logo.png", main_png)
        
        sec_file_id = None
        if sec_png:
            print("1b) Uploading SECONDARY logo…")
            sec_file_id, _ = upload_png_to_shopify_files(f"{handle}_secondary.png", sec_png)

        print("2) Ensuring smart collection exists…")
        collection_gid = ensure_smart_collection(handle=handle, title=f"{store_name} Storefront")
        
        print("2b) Publishing collection…")
        warn = publish_collection_to_online_store(collection_gid)
        if warn: print(f" ⚠️ {warn}")

        print("3) Upserting custom_shop metaobject…")
        meta_id = upsert_custom_shop_metaobject(
            store_handle=handle, store_name=store_name, owner_customer_id=customer_id,
            collection_gid=collection_gid, collection_handle=handle,
            main_logo_file_id=main_file_id, secondary_logo_file_id=sec_file_id, type_of_store=type_of_store
        )

        print("4) Tagging customer safely…")
        ensure_customer_storefront_tags(customer_id, handle)

        print("5) Triggering studio automation…")
        trigger_studio_automation(handle, main_url, store_name)

        print("=== PROVISION COMPLETE ===\n")
    except Exception as e:
        print(f"❌ PROVISIONING FAILED: {str(e)}")