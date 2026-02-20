# shopify_provision.py
# Run once to provision a store WITHOUT Make:
# - Read finished logo(s) from uploads/<session>_curr.png
# - Upload to Shopify Files
# - Create/update collection (private-store template, tag rule = handle)
# - Upsert custom_shop metaobject
# - Optionally trigger studio-automation service

import os
import re
import json
import time
import argparse
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

UPLOAD_DIR = "uploads"

# ----------------------------
# Metaobject config (EDIT IF NEEDED)
# ----------------------------
METAOBJECT_TYPE = "custom_shop"  # if your type is custom.custom_shop, change it here
FIELD_NAME = "name"
FIELD_LOGO = "logo"
FIELD_SECONDARY_LOGO = "secondary_logo"
FIELD_OWNER_CUSTOMER_ID = "owner_customer_id"
FIELD_COLLECTION_GID = "collection_gid"
FIELD_COLLECTION_HANDLE = "collection_handle"
FIELD_HANDLE = "handle"  # if you don't have this field, you can remove it from payload below

# Collection template to apply
COLLECTION_TEMPLATE_SUFFIX = "private-store"


# ----------------------------
# Env / Shopify helpers
# ----------------------------
def env_get(key: str, required: bool = True) -> str:
    v = os.getenv(key)
    if required and not v:
        raise RuntimeError(f"Missing {key} in environment/.env")
    return v or ""


def shopify_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    shop = env_get("SHOP")
    api_version = env_get("API_VERSION")
    token = env_get("CLIENT_SECRET")
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


def slugify(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def build_handle(store_name: str, owner_customer_id: str) -> str:
    # last 4 digits of customer id (or last 4 of string)
    digits = re.sub(r"\D", "", owner_customer_id)
    last4 = (digits[-4:] if len(digits) >= 4 else owner_customer_id[-4:]).lower()
    base = slugify(store_name)
    return f"{base}-{last4}"


def session_curr_path(session_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{session_id}_curr.png")


# ----------------------------
# Shopify Files upload (stagedUploadsCreate -> PUT -> fileCreate)
# Returns: (file_id, file_url)
# ----------------------------
def upload_png_to_shopify_files(filename: str, png_bytes: bytes) -> Tuple[str, str]:
    # 1) staged upload target
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
    variables1 = {
        "input": [
            {
                "resource": "FILE",
                "filename": filename,
                "mimeType": "image/png",
                "httpMethod": "POST",
            }
        ]
    }
    d1 = shopify_graphql(q1, variables1)
    errs = d1["stagedUploadsCreate"]["userErrors"]
    if errs:
        raise RuntimeError(f"stagedUploadsCreate userErrors: {errs}")

    target = d1["stagedUploadsCreate"]["stagedTargets"][0]
    upload_url = target["url"]
    params = {p["name"]: p["value"] for p in target["parameters"]}
    resource_url = target["resourceUrl"]  # this becomes the "originalSource" for fileCreate

    # 2) multipart POST to upload_url
    files = {"file": (filename, png_bytes, "image/png")}
    r = requests.post(upload_url, data=params, files=files, timeout=120)
    r.raise_for_status()

    # 3) fileCreate
    q2 = """
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          ... on MediaImage {
            id
            image { url }
          }
          ... on GenericFile {
            id
            url
          }
        }
        userErrors { field message }
      }
    }
    """
    variables2 = {"files": [{"originalSource": resource_url, "contentType": "IMAGE"}]}
    d2 = shopify_graphql(q2, variables2)

    errs2 = d2["fileCreate"]["userErrors"]
    if errs2:
        raise RuntimeError(f"fileCreate userErrors: {errs2}")

    created = d2["fileCreate"]["files"][0]
    file_id = created["id"]
    file_url = None

    # MediaImage vs GenericFile shape
    if "image" in created and created["image"] and created["image"].get("url"):
        file_url = created["image"]["url"]
    elif created.get("url"):
        file_url = created["url"]

    return file_id, file_url or ""


# ----------------------------
# Collection create/update
# - smart collection rule: Tag equals handle
# - template suffix = private-store
# ----------------------------
def ensure_collection(handle: str, title: str) -> Tuple[str, str]:
    # Try find existing by handle
    q_find = """
    query getCollectionByHandle($handle: String!) {
      collectionByHandle(handle: $handle) {
        id
        handle
        title
      }
    }
    """
    existing = shopify_graphql(q_find, {"handle": handle}).get("collectionByHandle")

    if existing and existing.get("id"):
        col_id = existing["id"]
        # Update title + template suffix (and keep handle same)
        q_upd = """
        mutation collectionUpdate($input: CollectionInput!) {
          collectionUpdate(input: $input) {
            collection { id handle title }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": {
                "id": col_id,
                "title": title,
                "templateSuffix": COLLECTION_TEMPLATE_SUFFIX,
            }
        }
        d = shopify_graphql(q_upd, variables)
        errs = d["collectionUpdate"]["userErrors"]
        if errs:
            raise RuntimeError(f"collectionUpdate userErrors: {errs}")
        return d["collectionUpdate"]["collection"]["id"], handle

    # Create NEW smart collection with rule Tag == handle
    q_create = """
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
            "ruleSet": {
                "appliedDisjunctively": False,
                "rules": [
                    {
                        "column": "TAG",
                        "relation": "EQUALS",
                        "condition": handle
                    }
                ]
            },
            "templateSuffix": COLLECTION_TEMPLATE_SUFFIX,
        }
    }
    d = shopify_graphql(q_create, variables)
    errs = d["collectionCreate"]["userErrors"]
    if errs:
        raise RuntimeError(f"collectionCreate userErrors: {errs}")

    col = d["collectionCreate"]["collection"]
    return col["id"], col["handle"]


# ----------------------------
# Metaobject upsert (create or update by handle)
# ----------------------------
def upsert_custom_shop_metaobject(
    store_handle: str,
    store_name: str,
    owner_customer_id: str,
    collection_gid: str,
    collection_handle: str,
    main_logo_file_id: str,
    secondary_logo_file_id: Optional[str] = None,
) -> str:
    # Find existing metaobject by handle (if your metaobject "handle" is different, adjust query)
    q_find = """
    query findMetaobject($type: String!, $handle: String!) {
      metaobjectByHandle(handle: {type: $type, handle: $handle}) {
        id
        handle
      }
    }
    """
    found = shopify_graphql(q_find, {"type": METAOBJECT_TYPE, "handle": store_handle}).get("metaobjectByHandle")
    existing_id = found["id"] if found else None

    fields = [
        {"key": FIELD_NAME, "value": store_name},
        {"key": FIELD_OWNER_CUSTOMER_ID, "value": owner_customer_id},
        {"key": FIELD_COLLECTION_GID, "value": collection_gid},
        {"key": FIELD_COLLECTION_HANDLE, "value": collection_handle},
        {"key": FIELD_LOGO, "value": main_logo_file_id},
    ]

    # Optional: store handle as a field too (only if your metaobject has it)
    if FIELD_HANDLE:
        fields.append({"key": FIELD_HANDLE, "value": store_handle})

    if secondary_logo_file_id:
        fields.append({"key": FIELD_SECONDARY_LOGO, "value": secondary_logo_file_id})

    q_upsert = """
    mutation metaobjectUpsert($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
        metaobject { id handle type }
        userErrors { field message }
      }
    }
    """

    variables = {
        "handle": {"type": METAOBJECT_TYPE, "handle": store_handle},
        "metaobject": {
            "fields": fields,
            # If you have a definition that requires capabilities, leave it out here.
        },
    }

    d = shopify_graphql(q_upsert, variables)
    errs = d["metaobjectUpsert"]["userErrors"]
    if errs:
        raise RuntimeError(f"metaobjectUpsert userErrors: {errs}")

    return d["metaobjectUpsert"]["metaobject"]["id"]


# ----------------------------
# Optional: trigger studio automation
# ----------------------------
def trigger_studio_automation(handle: str) -> None:
    url = os.getenv("STUDIO_AUTOMATION_URL", "").strip()
    if not url:
        print("ℹ️ STUDIO_AUTOMATION_URL not set — skipping trigger.")
        return

    token = os.getenv("STUDIO_AUTOMATION_TOKEN", "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {"handle": handle}
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Studio automation trigger failed {r.status_code}: {r.text}")
    print("✅ Studio automation triggered.")


def main():
    load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="Storefront name (human readable)")
    ap.add_argument("--owner_customer_id", required=True, help="Owner customer id (used for last4)")
    ap.add_argument("--main_session_id", required=True, help="Uploader session id for MAIN logo")
    ap.add_argument("--secondary_session_id", default="", help="Uploader session id for SECONDARY logo (optional)")
    args = ap.parse_args()

    store_name = args.name.strip()
    owner_customer_id = args.owner_customer_id.strip()
    handle = build_handle(store_name, owner_customer_id)

    print(f"\n=== PROVISION START ===")
    print(f"Name: {store_name}")
    print(f"Owner Customer ID: {owner_customer_id}")
    print(f"Handle: {handle}")

    # Read main logo bytes from uploads/
    main_path = session_curr_path(args.main_session_id)
    if not os.path.exists(main_path):
        raise RuntimeError(f"Main session file not found: {main_path}")

    with open(main_path, "rb") as f:
        main_png = f.read()

    # Optional secondary
    secondary_file_id = None
    if args.secondary_session_id:
        sec_path = session_curr_path(args.secondary_session_id)
        if not os.path.exists(sec_path):
            raise RuntimeError(f"Secondary session file not found: {sec_path}")
        with open(sec_path, "rb") as f:
            sec_png = f.read()
    else:
        sec_png = None

    # 1) Upload files to Shopify Files
    print("\n1) Uploading MAIN logo to Shopify Files…")
    main_file_id, main_url = upload_png_to_shopify_files(f"{handle}_logo.png", main_png)
    print(f"✅ Main file id: {main_file_id}")
    if main_url:
        print(f"   Main url: {main_url}")

    if sec_png:
        print("\n1b) Uploading SECONDARY logo to Shopify Files…")
        secondary_file_id, sec_url = upload_png_to_shopify_files(f"{handle}_secondary.png", sec_png)
        print(f"✅ Secondary file id: {secondary_file_id}")
        if sec_url:
            print(f"   Secondary url: {sec_url}")

    # 2) Create/update collection
    print("\n2) Ensuring collection exists (template private-store, rule tag == handle)…")
    collection_gid, collection_handle = ensure_collection(handle=handle, title=f"{store_name} Storefront")
    print(f"✅ Collection gid: {collection_gid}")
    print(f"✅ Collection handle: {collection_handle}")

    # 3) Upsert metaobject
    print("\n3) Upserting custom_shop metaobject…")
    meta_id = upsert_custom_shop_metaobject(
        store_handle=handle,
        store_name=store_name,
        owner_customer_id=owner_customer_id,
        collection_gid=collection_gid,
        collection_handle=collection_handle,
        main_logo_file_id=main_file_id,
        secondary_logo_file_id=secondary_file_id,
    )
    print(f"✅ Metaobject id: {meta_id}")

    # 4) Trigger studio automation (optional)
    print("\n4) Triggering studio automation (optional)…")
    trigger_studio_automation(handle)

    print("\n=== PROVISION COMPLETE ===\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n❌ FAILED:", str(e))
        raise
