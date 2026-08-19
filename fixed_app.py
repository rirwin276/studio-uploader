from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

import app as core
import request_mode_app as wrapped

app = wrapped.app
log = logging.getLogger(__name__)

_CACHE_TTL = 8.0
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()
_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    app.router.routes = [
        route for route in app.router.routes
        if not (
            getattr(route, "path", None) == path
            and method in (getattr(route, "methods", set()) or set())
        )
    ]


# Replace the old async endpoint. It called requests.post directly on the event
# loop and returned neither handle nor tags, which the Admin Powers editor logic
# needs to recognize products and build edit URLs.
_remove_route("/admin/store/{handle}/products", "GET")

_QUERY = """
query AdminStoreProducts($query: String!, $after: String) {
  products(first: 50, query: $query, after: $after, sortKey: UPDATED_AT, reverse: true) {
    edges {
      node {
        id
        handle
        title
        status
        tags
        featuredImage { url }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _cache_get(handle: str) -> list[dict[str, Any]] | None:
    with _CACHE_LOCK:
        item = _CACHE.get(handle)
        if not item:
            return None
        saved_at, products = item
        if time.monotonic() - saved_at > _CACHE_TTL:
            _CACHE.pop(handle, None)
            return None
        return [dict(product) for product in products]


def _cache_set(handle: str, products: list[dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        _CACHE[handle] = (time.monotonic(), [dict(product) for product in products])


def _graphql_products(search: str, cursor: str | None) -> Dict[str, Any]:
    if not core._SHOPIFY_SHOP or not core._SHOPIFY_ACCESS_TOKEN:
        raise RuntimeError("Shopify Admin API credentials not configured (SHOP / CLIENT_SECRET)")

    url = f"https://{core._SHOPIFY_SHOP}/admin/api/{core._SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": core._SHOPIFY_ACCESS_TOKEN,
        "Connection": "close",
    }
    payload = {"query": _QUERY, "variables": {"query": search, "after": cursor}}
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(5, 20))
            if (response.status_code == 429 or response.status_code >= 500) and attempt == 0:
                time.sleep(0.75)
                continue
            response.raise_for_status()
            parsed = response.json()
            if parsed.get("errors"):
                raise RuntimeError("Shopify GraphQL errors: " + json.dumps(parsed["errors"], separators=(",", ":")))
            data = parsed.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("Shopify GraphQL returned no data")
            return data
        except Exception as exc:
            last_error = exc
            if attempt == 0 and isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                time.sleep(0.5)
                continue
            break

    raise RuntimeError(str(last_error or "Shopify products request failed"))


@app.get("/admin/store/{handle}/products")
def admin_store_list_products_fixed(handle: str, request: Request):
    denied = core._require_admin_secret(request)
    if denied is not None:
        return denied

    handle = (handle or "").strip().lower()
    if not _SAFE_HANDLE.fullmatch(handle):
        return JSONResponse({"error": "invalid store handle"}, status_code=400)

    cached = _cache_get(handle)
    if cached is not None:
        return {"handle": handle, "products": cached, "cached": True}

    started = time.monotonic()
    products: list[dict[str, Any]] = []
    cursor: str | None = None

    try:
        while True:
            data = _graphql_products(f"tag:{handle}", cursor)
            connection = data.get("products") or {}
            for edge in connection.get("edges") or []:
                node = (edge or {}).get("node") or {}
                if not node.get("id"):
                    continue
                status = str(node.get("status") or "").upper()
                products.append({
                    "id": node["id"],
                    "handle": node.get("handle") or "",
                    "title": node.get("title") or "",
                    "status": status,
                    "hidden": status != "ACTIVE",
                    "featured_image": (node.get("featuredImage") or {}).get("url"),
                    "tags": node.get("tags") or [],
                })

            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            cursor = str(page_info["endCursor"])

        _cache_set(handle, products)
        log.info("admin products loaded handle=%s count=%s elapsed=%.3fs", handle, len(products), time.monotonic() - started)
        return {"handle": handle, "products": products, "cached": False}
    except Exception as exc:
        log.exception("admin products failed handle=%s elapsed=%.3fs type=%s", handle, time.monotonic() - started, type(exc).__name__)
        return JSONResponse({
            "error": "Unable to load store products from Shopify",
            "detail": str(exc)[:300],
        }, status_code=502)


# Command Center metrics and storefront activity are isolated from the upload
# pipeline so they can be tested and rolled back without touching image flows.
try:
    from command_center import install_command_center_routes

    install_command_center_routes(app, core)
except Exception:
    log.exception("Could not install Command Center routes")
