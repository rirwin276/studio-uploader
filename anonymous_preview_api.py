"""Bearer-scoped view of draft products; no cart, invitation, or member APIs."""
from __future__ import annotations
import hmac
import json
import os
import re
import threading
import time
from urllib.parse import urlencode
import requests
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
import anonymous_demo as demo
import outreach_tracking

MARKER = "ss-anonymous-demo"
HIDDEN = "ss-preview-hidden"


def gateway(method, path, payload=None):
    base = os.getenv("ANONYMOUS_PREVIEW_BUILDER_URL", "").rstrip("/")
    secret = os.getenv("ANONYMOUS_PREVIEW_GATEWAY_SECRET", "")
    if not base.startswith("https://") or len(secret) < 32:
        raise HTTPException(503, "Preview design service is not configured")
    response = requests.request(method, base + path, json=payload,
        headers={"X-Preview-Gateway": secret}, timeout=45)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", "Preview design service is unavailable")
        except Exception:
            detail = "Preview design service is unavailable"
        raise HTTPException(response.status_code if response.status_code < 500 else 503, detail)
    return response.json()


def builder_ready():
    try:
        result = gateway("GET", "/api/anonymous-preview/capabilities")
        return result.get("draft_only") is True and result.get("version") == 1
    except Exception:
        return False


def authorize(core, request, *, edit=False):
    token = demo._bearer(request)
    try:
        claims = demo._verify_token(token)
    except ValueError:
        raise HTTPException(401, "Open this preview in the browser where you created it")
    handle = str(claims["h"])
    state = outreach_tracking.read(core, handle)
    if not outreach_tracking.is_anonymous_demo_source(state.get("source")) or not hmac.compare_digest(
        str(state.get("resume_token_hash") or ""), demo._token_hash(token)
    ):
        raise HTTPException(403, "This session cannot access that store")
    if state.get("status") in {"deleted", "deleting"}:
        raise HTTPException(410, "This preview has expired")
    shop = core._get_custom_shop(handle)
    if not shop:
        raise HTTPException(409, "Your store is still being prepared")
    fields = shop.get("fields") or {}
    owner = core._normalize_store_owner(fields.get("owner_customer_id") or "")
    marker = core._get_collection_claim_owner(fields["collection_gid"]) if fields.get("collection_gid") else ""
    from anonymous_lifecycle import is_deletion_claim
    if is_deletion_claim(owner) or is_deletion_claim(marker):
        raise HTTPException(410, "This preview has expired")
    if edit:
        if not demo._origin_allowed(request):
            raise HTTPException(403, "Open your preview on Stella & Sage")
        if owner or marker or state.get("claim_status") == "claimed":
            raise HTTPException(409, "This store has been claimed. Open its normal admin page.")
    return handle, state, shop


PRODUCT_QUERY = """query PrivatePreviewProducts($query: String!, $after: String) {
  products(first: 50, query: $query, after: $after) {
    nodes { id handle title status tags description
      images(first: 12) { nodes { url altText } }
      variants(first: 100) { nodes { id title price selectedOptions { name value } image { url } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""


def products(core, handle):
    result, after = [], None
    while True:
        data = core._shopify_graphql(PRODUCT_QUERY, {"query": f"tag:{handle}", "after": after})["products"]
        for product in data.get("nodes") or []:
            if handle in product.get("tags", []) and MARKER in product.get("tags", []):
                result.append(product)
        page = data.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return result
        after = page["endCursor"]


def mutate(core, query, variables, field):
    result = core._shopify_graphql(query, variables).get(field) or {}
    if result.get("userErrors"):
        raise HTTPException(502, "Shopify could not save that change. Please retry.")
    return result


def activate_claimed(core, handle, shop=None):
    """Publish only after Shopify has an actual numeric customer owner."""
    shop = shop or core._get_custom_shop(handle)
    fields = (shop or {}).get("fields") or {}
    owner = core._normalize_store_owner(fields.get("owner_customer_id") or "")
    if not str(owner).isdigit():
        return False
    # Don't publish incomplete initial builds or a design still being rendered.
    state = outreach_tracking.read(core, handle)
    if str(fields.get("is_fully_ready") or "").lower() != "true":
        return False
    if (state.get("prospect_demo") or {}).get("product_status") in {"reserved", "building"}:
        return False
    for product in products(core, handle):
        if HIDDEN in product.get("tags", []) or product.get("status") == "ACTIVE":
            continue
        product_id = str(product["id"]).rsplit("/", 1)[-1]
        if not product_id.isdigit():
            raise HTTPException(502, "Invalid product identifier")
        core._shopify_rest_put(f"products/{product_id}.json", {"product": {"id": int(product_id), "status": "active", "published": True}})
    outreach_tracking.update(core, handle, {"claim_status": "claimed", "status": "claimed",
        "store_status": "claimed", "expires_at": None, "claimed_customer_id": owner})
    return True


def reconcile(core):
    """Restart-safe reconciliation also handles a claim made in another tab."""
    for handle, state in outreach_tracking.list_all(core).items():
        if outreach_tracking.is_anonymous_demo_source(state.get("source")) and state.get("status") not in {"deleted", "deleting"}:
            try:
                if not activate_claimed(core, handle) and state.get("status") in {"queued", "building"}:
                    demo._refresh_readiness(core, handle)
            except Exception:
                # Leave state retryable; never mark a partially published store complete.
                pass


def install(app, core):
    @app.get("/api/demo/store")
    def store(request: Request):
        handle, state, shop = authorize(core, request)
        if activate_claimed(core, handle, shop):
            return JSONResponse({"claimed": True, "store_url": f"https://stellasageco.com/collections/{handle}",
                "admin_url": f"https://stellasageco.com/pages/admin-powers?shop={handle}"}, headers={"Cache-Control": "no-store"})
        fields = shop.get("fields") or {}
        logo_data = core._shopify_graphql("""query PreviewLogo($id: ID!) {
          metaobject(id: $id) { field(key: "logo") { reference { ... on MediaImage { image { url } } } } }
        }""", {"id": shop["id"]})
        reference = ((logo_data.get("metaobject") or {}).get("field") or {}).get("reference") or {}
        logo = (reference.get("image") or {}).get("url", "")
        try:
            appearance = json.loads(fields.get("storefront_settings") or "{}")
        except Exception:
            appearance = {}
        return JSONResponse({"handle": handle, "name": fields.get("name") or state.get("organization_name") or handle,
            "logo_url": logo, "appearance": appearance, "products": products(core, handle),
            "build": demo._public_status(handle, state), "draft_only": True}, headers={"Cache-Control": "no-store"})

    @app.get("/api/demo/catalog")
    def catalog(request: Request):
        handle, _, _ = authorize(core, request, edit=True)
        data = gateway("GET", f"/api/anonymous-preview/{handle}/catalog")
        base = os.environ["ANONYMOUS_PREVIEW_BUILDER_URL"].rstrip("/")
        for item in data.get("products", []):
            item["preview_image"] = base + str(item["route"]) + "/card-image"
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.post("/api/demo/builder")
    async def builder(request: Request):
        handle, _, _ = authorize(core, request, edit=True)
        body = await request.json()
        if len(products(core, handle)) >= 60 and not body.get("product_handle"):
            raise HTTPException(409, "This preview has 60 products. Remove one to make room for a new design.")
        data = gateway("POST", f"/api/anonymous-preview/{handle}/builder", body)
        base = os.environ["ANONYMOUS_PREVIEW_BUILDER_URL"].rstrip("/")
        path = str(data.get("path") or "")
        if not path.startswith("/editor/"):
            raise HTTPException(502, "Builder returned an invalid destination")
        return JSONResponse({"url": base + path}, headers={"Cache-Control": "no-store"})

    @app.post("/api/demo/appearance")
    async def appearance(request: Request):
        handle, _, shop = authorize(core, request, edit=True)
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 120:
            raise HTTPException(400, "Use a store name between 1 and 120 characters")
        try:
            settings = json.loads(shop["fields"].get("storefront_settings") or "{}")
        except Exception:
            settings = {}
        for key in ("primary_color", "secondary_color"):
            color = str(body.get(key) or "")
            if not re.fullmatch(r"#[a-fA-F0-9]{6}", color):
                raise HTTPException(400, "Choose a valid color")
            settings[key] = color
        from outreach_appearance import _contrast
        settings.update(primary_text=_contrast(settings["primary_color"]),
            secondary_text=_contrast(settings["secondary_color"]),
            welcome_message=str(body.get("welcome_message") or "")[:180], enabled=True)
        mutate(core, """mutation PreviewAppearance($id: ID!, $metaobject: MetaobjectUpdateInput!) {
          metaobjectUpdate(id: $id, metaobject: $metaobject) { metaobject { id } userErrors { message } }
        }""", {"id": shop["id"], "metaobject": {"fields": [{"key": "name", "value": name},
            {"key": "storefront_settings", "value": json.dumps(settings)}]}}, "metaobjectUpdate")
        return {"ok": True}

    @app.post("/api/demo/product")
    async def product_change(request: Request):
        handle, _, _ = authorize(core, request, edit=True)
        body = await request.json()
        product = next((p for p in products(core, handle) if p["id"] == body.get("id")), None)
        if not product:
            raise HTTPException(403, "Product is outside this preview")
        if body.get("action") == "delete":
            mutate(core, """mutation PreviewDelete($input: ProductDeleteInput!) {
              productDelete(input: $input) { deletedProductId userErrors { message } }
            }""", {"input": {"id": product["id"]}}, "productDelete")
        elif body.get("action") in {"hide", "show"}:
            tags = [t for t in product["tags"] if t != HIDDEN]
            if body["action"] == "hide":
                tags.append(HIDDEN)
            mutate(core, """mutation PreviewVisibility($input: ProductInput!) {
              productUpdate(input: $input) { product { id } userErrors { message } }
            }""", {"input": {"id": product["id"], "tags": tags, "status": "DRAFT"}}, "productUpdate")
        else:
            raise HTTPException(400, "Unknown product action")
        return {"ok": True}

    if os.getenv("ANONYMOUS_PREVIEW_RECONCILE_ENABLED", "").lower() == "true":
        def worker():
            while True:
                try:
                    reconcile(core)
                except Exception:
                    pass
                time.sleep(30)
        threading.Thread(target=worker, daemon=True, name="anonymous-preview-reconcile").start()
