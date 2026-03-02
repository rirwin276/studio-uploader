# shopify_provision.py — Shopify provisioning for Studio Uploader (vNext, handle-safe)
# - Creates/updates Smart Collection (template suffix = private-store)
# - Uploads main/secondary logos to Shopify Files (as IMAGE when possible)
# - Tags customer with store handle (MERGE-SAFE; never deletes old tags)
#   REQUIRED tags:
#     storefront-admin--{handle}
#     storefront-member--{handle}
# - Upserts custom_shop metaobject with required fields (+ optional primary_color)
# - Publishes collection to all publications
# - Triggers Studio Automation ALWAYS (required)
# - Triggers Printful Automation (optional but enabled via env)
#
# IMPORTANT: This file matches the app.py subprocess contract:
#   python shopify_provision.py --name ... --handle ... --owner_customer_id ... --main_session_id ... --uploads_dir ...
#
# Key FIXES:
# 1) metaobjectUpsert: MetaobjectUpsertInput DOES NOT accept "type" field -> remove it.
#    Type is only provided in MetaobjectHandleInput ("handle" variable).
# 2) customer tagging adds BOTH admin+member tags.
# 3) smart collection rule enums forced to TAG/EQUALS etc.
#
# NEW: Printful Automation trigger
# - Set env PRINTFUL_AUTOMATION_URL to enable (recommended):
#     PRINTFUL_AUTOMATION_URL=https://printfulautomation-production.up.railway.app/run
# - Optional bearer:
#     PRINTFUL_AUTOMATION_TOKEN=...

from __future__ import annotations

import os
import re
import json
import time
import argparse
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

import requests


# -----------------------------
# ENV
# -----------------------------
def env_get(name: str, required: bool = True, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if required and (v is None or str(v).strip() == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return str(v).strip()


SHOP = env_get("SHOP", required=True)  # e.g. stellaandsage.myshopify.com
API_VERSION = env_get("API_VERSION", required=True)  # e.g. 2026-01
ACCESS_TOKEN = env_get("CLIENT_SECRET", required=True)  # Admin API access token

# REQUIRED: always called
STUDIO_AUTOMATION_URL = env_get("STUDIO_AUTOMATION_URL", required=True)
STUDIO_AUTOMATION_TOKEN = os.getenv("STUDIO_AUTOMATION_TOKEN", "").strip()  # optional bearer token

# OPTIONAL: Printful Automation (enabled if URL is set)
PRINTFUL_AUTOMATION_URL = os.getenv("PRINTFUL_AUTOMATION_URL", "").strip()
PRINTFUL_AUTOMATION_TOKEN = os.getenv("PRINTFUL_AUTOMATION_TOKEN", "").strip()  # optional bearer token

METAOBJECT_TYPE = os.getenv("METAOBJECT_TYPE", "custom_shop").strip()
COLLECTION_TEMPLATE_SUFFIX = os.getenv("COLLECTION_TEMPLATE_SUFFIX", "private-store").strip()

# Smart collection rule config (Shopify expects ENUMS)
COLLECTION_RULE_FIELD = os.getenv("COLLECTION_RULE_FIELD", "TAG").strip()           # TAG / TITLE / ...
COLLECTION_RULE_RELATION = os.getenv("COLLECTION_RULE_RELATION", "EQUALS").strip()  # EQUALS / CONTAINS / ...

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
FILE_READY_MAX_WAIT_S = int(os.getenv("FILE_READY_MAX_WAIT_S", "120"))
PUBLISH_RETRY_S = float(os.getenv("PUBLISH_RETRY_S", "1.5"))
PUBLISH_RETRY_MAX = int(os.getenv("PUBLISH_RETRY_MAX", "5"))


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
def slugify_handle(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "store"


def ensure_gid_customer(customer_id: str) -> str:
    cid = customer_id.strip()
    if cid.startswith("gid://"):
        return cid
    cid_num = cid.split("/")[-1]
    return f"gid://shopify/Customer/{cid_num}"


def normalize_customer_id_value(customer_id: str) -> str:
    """
    Your metaobject field 'owner_customer_id' is a Single line text field.
    Store the numeric ID as text (clean + consistent).
    """
    cid = (customer_id or "").strip()
    if cid.startswith("gid://"):
        return cid.split("/")[-1]
    return cid.split("/")[-1]


def read_session_png(uploads_dir: Path, session_id: str) -> bytes:
    # app.py writes uploads/<session>_curr.png
    p = uploads_dir / f"{session_id}_curr.png"
    if not p.exists():
        raise RuntimeError(f"Missing session image: {p}")
    return p.read_bytes()


def _normalize_rule_enum(val: str, kind: str) -> str:
    """
    Shopify Collection ruleSet expects ENUMS (TAG, EQUALS, etc).
    We accept lowercase env values and normalize them safely.
    """
    v = (val or "").strip()
    if not v:
        return "TAG" if kind == "column" else "EQUALS"
    v_up = v.upper()

    if kind == "column":
        mapping = {
            "TAG": "TAG",
            "TITLE": "TITLE",
            "TYPE": "TYPE",
            "VENDOR": "VENDOR",
            "VARIANT_PRICE": "VARIANT_PRICE",
        }
        if v_up in mapping:
            return mapping[v_up]
        return v_up

    if kind == "relation":
        mapping = {
            "EQUALS": "EQUALS",
            "CONTAINS": "CONTAINS",
            "NOT_EQUALS": "NOT_EQUALS",
            "NOT_CONTAINS": "NOT_CONTAINS",
            "STARTS_WITH": "STARTS_WITH",
            "ENDS_WITH": "ENDS_WITH",
            "IS_SET": "IS_SET",
            "IS_NOT_SET": "IS_NOT_SET",
            "GREATER_THAN": "GREATER_THAN",
            "LESS_THAN": "LESS_THAN",
        }
        if v_up in mapping:
            return mapping[v_up]
        return v_up

    return v_up


def customer_add_tags(customer_gid: str, new_tags: List[str]) -> None:
    """
    Merge-safe customer tag add:
    - Fetches existing tags
    - Adds new tags if missing
    - Updates customer with merged tags (never deletes old tags)
    """
    cleaned: List[str] = []
    for t in (new_tags or []):
        t2 = (t or "").strip()
        if t2:
            cleaned.append(t2)

    if not cleaned:
        return

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
        raise RuntimeError(f"Customer not found: {customer_gid}")

    existing = cust.get("tags") or []
    merged = list(existing)

    added_any = False
    for t in cleaned:
        if t not in merged:
            merged.append(t)
            added_any = True

    if not added_any:
        print(f"🏷️ Customer already has tags: {', '.join(cleaned)}")
        return

    q_update = """
    mutation customerUpdate($input: CustomerInput!) {
      customerUpdate(input: $input) {
        customer { id tags }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql(q_update, {"input": {"id": customer_gid, "tags": merged}})["customerUpdate"]
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"customerUpdate userErrors: {json.dumps(errs, indent=2)}")

    print(f"✅ Customer tagged: {', '.join(cleaned)}")


# -----------------------------
# Shopify Files upload (staged uploads)
# -----------------------------
def staged_upload_create(filename: str, mime_type: str) -> Dict[str, Any]:
    q = """
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
    variables = {
        "input": [{
            "resource": "FILE",
            "filename": filename,
            "mimeType": mime_type,
            "httpMethod": "POST",
        }]
    }
    data = shopify_graphql(q, variables)
    res = data["stagedUploadsCreate"]
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"stagedUploadsCreate userErrors: {json.dumps(errs, indent=2)}")
    targets = res.get("stagedTargets") or []
    if not targets:
        raise RuntimeError("stagedUploadsCreate returned no stagedTargets")
    return targets[0]


def staged_upload_post(target: Dict[str, Any], file_bytes: bytes, mime_type: str) -> str:
    url = target["url"]
    params = {p["name"]: p["value"] for p in target.get("parameters", [])}
    files = {"file": ("upload", file_bytes, mime_type)}
    r = requests.post(url, data=params, files=files, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return target["resourceUrl"]


def file_create_from_resource_url(resource_url: str, alt: str = "", prefer_image: bool = True) -> str:
    """
    Tries to create as IMAGE first, falls back to FILE.
    Returns file/media GID.
    """
    q = """
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          __typename
          ... on MediaImage { id image { url } }
          ... on GenericFile { id url }
        }
        userErrors { field message }
      }
    }
    """

    def _try(content_type: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        variables = {
            "files": [{
                "originalSource": resource_url,
                "alt": alt,
                "contentType": content_type,
            }]
        }
        data = shopify_graphql(q, variables)
        res = data["fileCreate"]
        errs = res.get("userErrors") or []
        files = res.get("files") or []
        if files:
            return files[0]["id"], errs
        return None, errs

    if prefer_image:
        file_id, errs = _try("IMAGE")
        if file_id and not errs:
            return file_id
        file_id2, errs2 = _try("FILE")
        if errs2:
            raise RuntimeError(f"fileCreate userErrors: {json.dumps(errs2, indent=2)}")
        if not file_id2:
            raise RuntimeError("fileCreate returned no files")
        return file_id2

    file_id, errs = _try("FILE")
    if errs:
        raise RuntimeError(f"fileCreate userErrors: {json.dumps(errs, indent=2)}")
    if not file_id:
        raise RuntimeError("fileCreate returned no files")
    return file_id


def file_wait_ready(file_id: str, max_wait_s: int = FILE_READY_MAX_WAIT_S) -> Dict[str, Any]:
    q = """
    query fileNode($id: ID!) {
      node(id: $id) {
        __typename
        ... on MediaImage {
          id
          image { url }
          createdAt
        }
        ... on GenericFile {
          id
          url
          createdAt
        }
      }
    }
    """
    start = time.time()
    last_node = None
    while True:
        data = shopify_graphql(q, {"id": file_id})
        node = data.get("node")
        last_node = node

        if node:
            if node.get("__typename") == "MediaImage":
                img = node.get("image") or {}
                if img.get("url"):
                    return node
            if node.get("__typename") == "GenericFile":
                if node.get("url"):
                    return node

        if time.time() - start > max_wait_s:
            raise RuntimeError(
                f"Timed out waiting for file to be ready: {file_id}\nLast node: {json.dumps(last_node, indent=2)}"
            )
        time.sleep(2)


def upload_png_to_shopify_files(png_bytes: bytes, filename: str, alt: str) -> Tuple[str, str]:
    target = staged_upload_create(filename=filename, mime_type="image/png")
    resource_url = staged_upload_post(target, png_bytes, mime_type="image/png")
    file_id = file_create_from_resource_url(resource_url, alt=alt, prefer_image=True)
    node = file_wait_ready(file_id)

    if node.get("__typename") == "MediaImage":
        return file_id, (node.get("image") or {}).get("url") or ""
    return file_id, node.get("url") or ""


# -----------------------------
# Collection create/update + publish
# -----------------------------
def collection_by_handle(handle: str) -> Optional[Dict[str, Any]]:
    q = """
    query collectionByHandle($handle: String!) {
      collectionByHandle(handle: $handle) {
        id
        handle
        title
        templateSuffix
        ruleSet {
          appliedDisjunctively
          rules { column relation condition }
        }
      }
    }
    """
    data = shopify_graphql(q, {"handle": handle})
    return data.get("collectionByHandle")


def smart_collection_rule_set(tag_value: str) -> Dict[str, Any]:
    col = _normalize_rule_enum(COLLECTION_RULE_FIELD, "column")
    rel = _normalize_rule_enum(COLLECTION_RULE_RELATION, "relation")
    return {
        "appliedDisjunctively": False,
        "rules": [{
            "column": col,
            "relation": rel,
            "condition": tag_value,
        }]
    }


def collection_create_smart(title: str, handle: str, tag_value: str) -> str:
    q = """
    mutation collectionCreate($input: CollectionInput!) {
      collectionCreate(input: $input) {
        collection { id handle title templateSuffix }
        userErrors { field message }
      }
    }
    """
    variables = {
        "input": {
            "title": title,
            "handle": handle,
            "templateSuffix": COLLECTION_TEMPLATE_SUFFIX,
            "ruleSet": smart_collection_rule_set(tag_value),
        }
    }
    data = shopify_graphql(q, variables)
    res = data["collectionCreate"]
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"collectionCreate userErrors: {json.dumps(errs, indent=2)}")
    col = res.get("collection")
    if not col:
        raise RuntimeError("collectionCreate returned no collection")
    return col["id"]


def collection_update_smart(collection_id: str, title: str, handle: str, tag_value: str) -> None:
    q = """
    mutation collectionUpdate($id: ID!, $input: CollectionInput!) {
      collectionUpdate(id: $id, input: $input) {
        collection { id handle title templateSuffix }
        userErrors { field message }
      }
    }
    """
    variables = {
        "id": collection_id,
        "input": {
            "title": title,
            "handle": handle,
            "templateSuffix": COLLECTION_TEMPLATE_SUFFIX,
            "ruleSet": smart_collection_rule_set(tag_value),
        }
    }
    data = shopify_graphql(q, variables)
    res = data["collectionUpdate"]
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"collectionUpdate userErrors: {json.dumps(errs, indent=2)}")


def get_all_publications() -> List[str]:
    q = """
    query pubs($first: Int!) {
      publications(first: $first) {
        edges { node { id name } }
      }
    }
    """
    data = shopify_graphql(q, {"first": 50})
    edges = data.get("publications", {}).get("edges") or []
    return [e["node"]["id"] for e in edges if e.get("node", {}).get("id")]


def publish_to_all_publications(publishable_id: str) -> None:
    pubs = get_all_publications()
    if not pubs:
        print("⚠️ No publications found; skipping publishablePublish.")
        return

    q = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }
    """
    variables = {"id": publishable_id, "input": [{"publicationId": pid} for pid in pubs]}

    last_err = None
    for attempt in range(1, PUBLISH_RETRY_MAX + 1):
        try:
            data = shopify_graphql(q, variables)
            errs = data["publishablePublish"].get("userErrors") or []
            if errs:
                raise RuntimeError(f"publishablePublish userErrors: {json.dumps(errs, indent=2)}")
            return
        except Exception as e:
            last_err = e
            if attempt < PUBLISH_RETRY_MAX:
                time.sleep(PUBLISH_RETRY_S * attempt)
                continue
            raise last_err


# -----------------------------
# Metaobject upsert
# -----------------------------
def metaobject_upsert_custom_shop(
    handle: str,
    name: str,
    logo_file_gid: str,
    owner_customer_id_text: str,
    collection_gid: str,
    collection_handle: str,
    secondary_logo_file_gid: Optional[str],
    type_of_store: Optional[str],
    is_fully_ready: bool,
    primary_color: Optional[str],
) -> str:
    q = """
    mutation metaobjectUpsert($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
        metaobject { id handle type }
        userErrors { field message }
      }
    }
    """

    primary_color_value = (primary_color or "").strip() or "No preference"

    fields = [
        {"key": "name", "value": name},
        {"key": "logo", "value": logo_file_gid},
        {"key": "owner_customer_id", "value": owner_customer_id_text},
        {"key": "collection_gid", "value": collection_gid},
        {"key": "collection_handle", "value": collection_handle},
        {"key": "is_fully_ready", "value": "true" if is_fully_ready else "false"},
        {"key": "primary_color", "value": primary_color_value},
    ]

    if secondary_logo_file_gid:
        fields.append({"key": "secondary_logo", "value": secondary_logo_file_gid})

    if type_of_store:
        fields.append({"key": "type_of_store", "value": type_of_store})

    variables = {
        "handle": {"type": METAOBJECT_TYPE, "handle": handle},
        "metaobject": {
            "fields": fields,
        }
    }

    data = shopify_graphql(q, variables)
    res = data["metaobjectUpsert"]
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"metaobjectUpsert userErrors: {json.dumps(errs, indent=2)}")

    mo = res.get("metaobject")
    if not mo:
        raise RuntimeError("metaobjectUpsert returned no metaobject")
    return mo["id"]


# -----------------------------
# Triggers
# -----------------------------
def _post_json(url: str, payload: Dict[str, Any], bearer_token: str = "") -> requests.Response:
    headers = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    return requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)


def trigger_studio_automation(payload: Dict[str, Any]) -> None:
    r = _post_json(STUDIO_AUTOMATION_URL, payload, bearer_token=STUDIO_AUTOMATION_TOKEN)
    if r.status_code >= 300:
        raise RuntimeError(
            f"Studio Automation trigger failed: HTTP {r.status_code}\n"
            f"URL: {STUDIO_AUTOMATION_URL}\n"
            f"Response: {r.text[:2000]}"
        )


def trigger_printful_automation(store_handle: str, type_of_store: str, primary_color: str) -> None:
    """
    Enabled if PRINTFUL_AUTOMATION_URL is set.
    Expecting Printful Automation service has endpoint:
      POST /run  JSON: {"store_handle":"...","type_of_store":"...","primary_color":"..."}
    """
    if not PRINTFUL_AUTOMATION_URL:
        print("ℹ️ PRINTFUL_AUTOMATION_URL not set — skipping Printful automation trigger.")
        return

    payload = {
        "store_handle": store_handle,
        "type_of_store": type_of_store or "",
        "primary_color": primary_color or "",
    }

    r = _post_json(PRINTFUL_AUTOMATION_URL, payload, bearer_token=PRINTFUL_AUTOMATION_TOKEN)
    if r.status_code >= 300:
        raise RuntimeError(
            f"Printful Automation trigger failed: HTTP {r.status_code}\n"
            f"URL: {PRINTFUL_AUTOMATION_URL}\n"
            f"Response: {r.text[:2000]}"
        )

    try:
        print("🧵 Printful Automation response:", r.json())
    except Exception:
        print("🧵 Printful Automation response (text):", r.text[:500])


# -----------------------------
# Main provisioning flow
# -----------------------------
def provision(
    storefront_name: str,
    storefront_handle: str,
    owner_customer_id: str,
    main_session_id: str,
    uploads_dir: Path,
    secondary_session_id: Optional[str] = None,
    type_of_store: Optional[str] = None,
    primary_color: Optional[str] = None,
) -> Dict[str, Any]:
    handle = (storefront_handle or "").strip()
    if not handle:
        handle = slugify_handle(storefront_name)

    tag_value = handle
    primary_color_value = (primary_color or "").strip() or "No preference"

    print(f"🧩 Provisioning handle: {handle}")
    print(f"🏷️ Collection tag rule: {_normalize_rule_enum(COLLECTION_RULE_FIELD,'column')} "
          f"{_normalize_rule_enum(COLLECTION_RULE_RELATION,'relation')} {tag_value}")
    print(f"🎨 Template suffix: {COLLECTION_TEMPLATE_SUFFIX}")
    print(f"🎨 primary_color (normalized): {repr(primary_color_value)}")
    if PRINTFUL_AUTOMATION_URL:
        print(f"🧵 Printful Automation enabled: {PRINTFUL_AUTOMATION_URL}")
    else:
        print("🧵 Printful Automation disabled (PRINTFUL_AUTOMATION_URL not set).")

    # 1) Upload main logo
    main_png = read_session_png(uploads_dir, main_session_id)
    main_file_gid, main_file_url = upload_png_to_shopify_files(
        main_png,
        filename=f"{handle}_logo.png",
        alt=f"{storefront_name} logo",
    )
    print("✅ Main logo uploaded:", main_file_gid, main_file_url)

    # 2) Upload secondary logo (optional)
    secondary_file_gid = None
    secondary_file_url = None
    if secondary_session_id:
        try:
            sec_png = read_session_png(uploads_dir, secondary_session_id)
            if sec_png and len(sec_png) > 100:
                secondary_file_gid, secondary_file_url = upload_png_to_shopify_files(
                    sec_png,
                    filename=f"{handle}_secondary_logo.png",
                    alt=f"{storefront_name} secondary logo",
                )
                print("✅ Secondary logo uploaded:", secondary_file_gid, secondary_file_url)
            else:
                print("⚠️ Secondary logo empty — skipping")
        except Exception as e:
            print("⚠️ Secondary logo failed — skipping:", str(e))

    # 3) Create or update collection
    existing = collection_by_handle(handle)
    if existing:
        collection_gid = existing["id"]
        print("♻️ Collection exists — updating:", collection_gid)
        collection_update_smart(collection_gid, storefront_name, handle, tag_value)
    else:
        print("🆕 Creating collection…")
        collection_gid = collection_create_smart(storefront_name, handle, tag_value)

    print("✅ Collection ready:", collection_gid)

    # 4) Publish to all publications
    publish_to_all_publications(collection_gid)
    print("✅ Collection published to all publications")

    # 5) Customer tags
    owner_customer_gid = ensure_gid_customer(owner_customer_id)
    admin_tag = f"storefront-admin--{handle}"
    member_tag = f"storefront-member--{handle}"
    customer_add_tags(owner_customer_gid, [admin_tag, member_tag])

    # 6) Upsert metaobject
    owner_customer_id_text = normalize_customer_id_value(owner_customer_id)
    metaobject_id = metaobject_upsert_custom_shop(
        handle=handle,
        name=storefront_name,
        logo_file_gid=main_file_gid,
        owner_customer_id_text=owner_customer_id_text,
        collection_gid=collection_gid,
        collection_handle=handle,
        secondary_logo_file_gid=secondary_file_gid,
        type_of_store=type_of_store,
        is_fully_ready=True,
        primary_color=primary_color_value,
    )
    print("✅ Metaobject upserted:", metaobject_id)

    # 7) Trigger Studio Automation ALWAYS (required)
    automation_payload = {
        "store_handle": handle,
        "collection_handle": handle,
        "collection_gid": collection_gid,
        "metaobject_type": METAOBJECT_TYPE,
        "storefront_name": storefront_name,
        "owner_customer_gid": owner_customer_gid,
        "owner_customer_id": owner_customer_id_text,
        "logo_file_gid": main_file_gid,
        "logo_url": main_file_url,
        "secondary_logo_file_gid": secondary_file_gid,
        "secondary_logo_url": secondary_file_url,
        "type_of_store": type_of_store,
        "primary_color": primary_color_value,
    }
    trigger_studio_automation(automation_payload)
    print("🚀 Studio Automation triggered successfully")

    # 8) Trigger Printful Automation (enabled via env)
    trigger_printful_automation(
        store_handle=handle,
        type_of_store=(type_of_store or ""),
        primary_color=primary_color_value,
    )
    print("🧵 Printful Automation trigger complete")

    return {
        "handle": handle,
        "collection_gid": collection_gid,
        "metaobject_id": metaobject_id,
        "logo_file_gid": main_file_gid,
        "logo_url": main_file_url,
        "secondary_logo_file_gid": secondary_file_gid,
        "secondary_logo_url": secondary_file_url,
        "type_of_store": type_of_store,
        "primary_color": primary_color_value,
        "customer_tags_added": [admin_tag, member_tag],
        "printful_automation_url": PRINTFUL_AUTOMATION_URL or "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="Storefront display name")
    ap.add_argument("--handle", required=True, help="Storefront handle (REQUIRED, passed from app.py)")
    ap.add_argument("--owner_customer_id", required=True, help="Customer gid or numeric id")
    ap.add_argument("--main_session_id", required=True, help="Session id for main logo (uploads/<id>_curr.png)")
    ap.add_argument("--uploads_dir", required=True, help="Uploads directory path")
    ap.add_argument("--secondary_session_id", default="", help="Optional session id for secondary logo")
    ap.add_argument("--type_of_store", default="", help="Optional type_of_store field")
    ap.add_argument("--primary_color", default="", help="Optional primary color (single-line text)")

    args = ap.parse_args()

    uploads_dir = Path(args.uploads_dir).resolve()
    if not uploads_dir.exists():
        raise RuntimeError(f"uploads_dir not found: {uploads_dir}")

    secondary_session_id = args.secondary_session_id.strip() or None
    type_of_store = args.type_of_store.strip() or None
    primary_color = args.primary_color.strip() or "No preference"

    result = provision(
        storefront_name=args.name.strip(),
        storefront_handle=args.handle.strip(),
        owner_customer_id=args.owner_customer_id.strip(),
        main_session_id=args.main_session_id.strip(),
        uploads_dir=uploads_dir,
        secondary_session_id=secondary_session_id,
        type_of_store=type_of_store,
        primary_color=primary_color,
    )

    print("========== PROVISION RESULT ==========")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()