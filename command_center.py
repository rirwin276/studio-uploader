"""Read-only Command Center metrics and privacy-minimized storefront activity.

The public browser never calls this service directly. Printful_Automation's
Shopify App Proxy relay verifies the signed Shopify request, then forwards the
request with the private X-Admin-Secret and verified identity headers.

Two routes are installed:

* POST /api/store/{handle}/activity/session
  Records one deduplicated storefront session. The raw browser session id is
  never persisted.
* GET /admin/command-center/summary
  Returns the all-store, super-admin-only operational rollup.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import outreach_tracking


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")
_ACTIVITY_TYPE = "store_activity"
_ACTIVITY_VERSION = 1
_RECENT_SESSION_LIMIT = 1000
_RECENT_ACTIVITY_LIMIT = 50
_SUMMARY_CACHE_TTL = max(60, int(os.getenv("COMMAND_CENTER_CACHE_SECONDS", "300")))
_MAX_ORDERS = max(50, int(os.getenv("COMMAND_CENTER_MAX_ORDERS", "5000")))
_ACTIVITY_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_SUMMARY_BUILD_LOCK = threading.Lock()
_SUMMARY_CACHE: tuple[float, Dict[str, Any]] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_handle(raw: str) -> str:
    handle = (raw or "").strip().lower()
    return handle if _SAFE_HANDLE.fullmatch(handle) else ""


def _normalized_customer_id(raw: str) -> str:
    value = (raw or "").strip()
    return value.split("/")[-1] if value else ""


def _decimal(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money_string(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _parse_datetime(raw: Any) -> Optional[datetime]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_days(raw: Any, *, now: Optional[datetime] = None) -> Optional[int]:
    parsed = _parse_datetime(raw)
    if parsed is None:
        return None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0, int((current - parsed).total_seconds() // 86400))


def _field_map(node: Dict[str, Any]) -> Dict[str, str]:
    return {
        str(field.get("key") or ""): str(field.get("value") or "")
        for field in (node.get("fields") or [])
        if field.get("key")
    }


def _page_all(core, query: str, root: str, variables: Optional[Dict[str, Any]] = None) -> list:
    """Collect nodes from a standard Shopify connection."""
    nodes: list = []
    cursor: Optional[str] = None
    base = dict(variables or {})
    while True:
        call_vars = dict(base)
        call_vars["after"] = cursor
        data = core._shopify_graphql(query, call_vars)
        connection = data.get(root) or {}
        nodes.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            return nodes
        cursor = str(page_info["endCursor"])


def _ensure_activity_definition(core) -> None:
    check = """
    query CommandCenterActivityDefinition($type: String!) {
      metaobjectDefinitionByType(type: $type) { id }
    }
    """
    data = core._shopify_graphql(check, {"type": _ACTIVITY_TYPE})
    if (data.get("metaobjectDefinitionByType") or {}).get("id"):
        return

    create = """
    mutation CreateCommandCenterActivityDefinition($definition: MetaobjectDefinitionCreateInput!) {
      metaobjectDefinitionCreate(definition: $definition) {
        metaobjectDefinition { id }
        userErrors { field message code }
      }
    }
    """
    result = core._shopify_graphql(create, {
        "definition": {
            "type": _ACTIVITY_TYPE,
            "name": "Store Activity",
            "fieldDefinitions": [
                {"key": "data", "name": "Data", "type": "json", "required": False},
            ],
        }
    })
    errors = ((result.get("metaobjectDefinitionCreate") or {}).get("userErrors")) or []
    if errors:
        raise RuntimeError("metaobjectDefinitionCreate userErrors: " + json.dumps(errors))


def _activity_state(core, handle: str) -> Dict[str, Any]:
    query = """
    query CommandCenterActivity($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) { fields { key value } }
    }
    """
    data = core._shopify_graphql(query, {"handle": {"type": _ACTIVITY_TYPE, "handle": handle}})
    node = data.get("metaobjectByHandle") or {}
    raw = _field_map(node).get("data") or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _store_exists(core, handle: str) -> bool:
    query = """
    query CommandCenterStoreExists($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) { id }
    }
    """
    data = core._shopify_graphql(query, {
        "handle": {
            "type": os.getenv("METAOBJECT_TYPE", "custom_shop").strip(),
            "handle": handle,
        }
    })
    return bool((data.get("metaobjectByHandle") or {}).get("id"))


def _save_activity_state(core, handle: str, state: Dict[str, Any]) -> None:
    mutation = """
    mutation SaveCommandCenterActivity($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
        metaobject { id handle }
        userErrors { field message code }
      }
    }
    """
    result = core._shopify_graphql(mutation, {
        "handle": {"type": _ACTIVITY_TYPE, "handle": handle},
        "metaobject": {"fields": [{"key": "data", "value": json.dumps(state, separators=(",", ":"))}]},
    })
    errors = ((result.get("metaobjectUpsert") or {}).get("userErrors")) or []
    if errors:
        raise RuntimeError("metaobjectUpsert userErrors: " + json.dumps(errors))


def _customer_display_name(core, customer_id: str) -> str:
    customer_id = _normalized_customer_id(customer_id)
    if not customer_id:
        return ""
    query = """
    query CommandCenterCustomer($id: ID!) {
      customer(id: $id) { displayName }
    }
    """
    try:
        data = core._shopify_graphql(query, {"id": f"gid://shopify/Customer/{customer_id}"})
        return str((data.get("customer") or {}).get("displayName") or "").strip()
    except Exception:
        return ""


def _apply_activity_event(
    state: Dict[str, Any],
    *,
    session_hash: str,
    occurred_at: str,
    path: str,
    customer_id: str,
    customer_display_name: str,
    is_super_admin: bool,
    role_known: bool = True,
) -> tuple[Dict[str, Any], bool]:
    """Pure update helper. Returns (updated_state, duplicate)."""
    out = dict(state or {})
    recent = [row for row in (out.get("recent_sessions") or []) if isinstance(row, dict)]
    if any(str(row.get("hash") or "") == session_hash for row in recent):
        return out, True

    recent.append({"hash": session_hash, "at": occurred_at})
    out["recent_sessions"] = recent[-_RECENT_SESSION_LIMIT:]
    out["version"] = _ACTIVITY_VERSION
    out.setdefault("tracking_started_at", occurred_at)
    out["total_sessions"] = int(out.get("total_sessions") or 0) + 1
    visitor_type = (
        "role_unknown"
        if not role_known
        else ("super_admin" if is_super_admin else ("customer" if customer_id else "anonymous"))
    )
    out["last_session"] = {
        "at": occurred_at,
        "path": path,
        "visitor_type": visitor_type,
    }
    recent_activity = [row for row in (out.get("recent_activity") or []) if isinstance(row, dict)]
    activity_event = {
        "event_type": "session",
        "at": occurred_at,
        "path": path,
        "visitor_type": visitor_type,
    }
    if customer_display_name and role_known and not is_super_admin:
        activity_event["customer_display_name"] = customer_display_name
    recent_activity.append(activity_event)
    out["recent_activity"] = recent_activity[-_RECENT_ACTIVITY_LIMIT:]

    if role_known and not is_super_admin:
        out["non_super_admin_sessions"] = int(out.get("non_super_admin_sessions") or 0) + 1
        last_other = {
            "at": occurred_at,
            "path": path,
            "visitor_type": "customer" if customer_id else "anonymous",
        }
        if customer_display_name:
            last_other["customer_display_name"] = customer_display_name
        out["last_non_super_admin_session"] = last_other

        if customer_id:
            out["last_authenticated_customer_activity"] = {
                "at": occurred_at,
                "path": path,
                "customer_display_name": customer_display_name or "Customer",
            }

    return out, False


def _store_nodes(core) -> list[Dict[str, Any]]:
    query = """
    query CommandCenterStores($type: String!, $after: String) {
      metaobjects(type: $type, first: 100, after: $after) {
        nodes { handle displayName fields { key value } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    return _page_all(core, query, "metaobjects", {"type": os.getenv("METAOBJECT_TYPE", "custom_shop").strip()})


def _collection_dates(core, collection_ids: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """Return collection timestamps used as the store provisioning timestamp.

    Provisioning creates the store's collection immediately before its
    ``custom_shop`` record. Reading the collection timestamp avoids adding a
    new field to the live store schema and keeps this monitoring feature
    completely read-only.
    """
    ids = sorted({
        str(collection_id).strip()
        for collection_id in collection_ids
        if str(collection_id).strip().startswith("gid://shopify/Collection/")
    })
    if not ids:
        return {}
    query = """
    query CommandCenterCollectionDates($ids: [ID!]!) {
      nodes(ids: $ids) {
        ... on Collection { id createdAt updatedAt }
      }
    }
    """
    result: Dict[str, Dict[str, str]] = {}
    for offset in range(0, len(ids), 250):
        data = core._shopify_graphql(query, {"ids": ids[offset:offset + 250]})
        for node in data.get("nodes") or []:
            if not isinstance(node, dict) or not node.get("id"):
                continue
            result[str(node["id"])] = {
                "created_at": str(node.get("createdAt") or ""),
                "updated_at": str(node.get("updatedAt") or ""),
            }
    return result


def _activity_nodes(core) -> Dict[str, Dict[str, Any]]:
    query = """
    query CommandCenterActivities($type: String!, $after: String) {
      metaobjects(type: $type, first: 100, after: $after) {
        nodes { handle fields { key value } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    nodes = _page_all(core, query, "metaobjects", {"type": _ACTIVITY_TYPE})
    result: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        handle = _normalized_handle(str(node.get("handle") or ""))
        raw = _field_map(node).get("data") or ""
        if not handle or not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                result[handle] = parsed
        except Exception:
            continue
    return result


def _access_scopes(core) -> set[str]:
    query = """
    query CommandCenterScopes {
      currentAppInstallation { accessScopes { handle } }
    }
    """
    data = core._shopify_graphql(query, {})
    return {
        str(scope.get("handle") or "").strip()
        for scope in ((data.get("currentAppInstallation") or {}).get("accessScopes") or [])
        if scope.get("handle")
    }


def _customer_nodes(core) -> list[Dict[str, Any]]:
    query = """
    query CommandCenterCustomers($after: String) {
      customers(first: 250, after: $after, sortKey: UPDATED_AT, reverse: true) {
        nodes { id displayName email tags updatedAt }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    return _page_all(core, query, "customers")


def _product_nodes(core) -> list[Dict[str, Any]]:
    query = """
    query CommandCenterProducts($after: String) {
      products(first: 250, after: $after, sortKey: UPDATED_AT, reverse: true) {
        nodes { id title handle status tags createdAt updatedAt }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    return _page_all(core, query, "products")


def _order_nodes(core) -> tuple[list[Dict[str, Any]], bool]:
    query = """
    query CommandCenterOrders($after: String) {
      orders(first: 50, after: $after, sortKey: CREATED_AT, reverse: true, query: "test:false") {
        nodes {
          id name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
          customer { displayName }
          lineItems(first: 10) {
            nodes {
              quantity currentQuantity
              originalTotalSet { shopMoney { amount currencyCode } }
              product { tags }
            }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    orders: list[Dict[str, Any]] = []
    cursor: Optional[str] = None
    truncated = False
    while True:
        data = core._shopify_graphql(query, {"after": cursor})
        connection = data.get("orders") or {}
        batch = connection.get("nodes") or []
        room = _MAX_ORDERS - len(orders)
        orders.extend(batch[:room])
        if len(batch) > room or len(orders) >= _MAX_ORDERS:
            truncated = bool(connection.get("pageInfo", {}).get("hasNextPage") or len(batch) > room)
            break
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = str(page_info["endCursor"])
    return orders, truncated


def _tag_handle(tag: str, prefixes: Iterable[str], handles: set[str]) -> str:
    value = (tag or "").strip().lower()
    for prefix in prefixes:
        if value.startswith(prefix):
            candidate = value[len(prefix):]
            if candidate in handles:
                return candidate
    return ""


def _admin_identity(customer: Dict[str, Any]) -> Dict[str, str]:
    return {
        "display_name": str(customer.get("displayName") or "").strip() or "Admin",
        "email": str(customer.get("email") or "").strip(),
    }


def _aggregate_customers(customers: list[Dict[str, Any]], handles: set[str]) -> tuple[Dict[str, Any], int, Dict[str, Any]]:
    by_store: Dict[str, Dict[str, Any]] = {
        handle: {"member_ids": set(), "admin_ids": set(), "admins": []}
        for handle in handles
    }
    unique_members: set[str] = set()
    platform_admin_ids: set[str] = set()
    platform_admins: list[Dict[str, str]] = []
    for customer in customers:
        cid = str(customer.get("id") or "")
        tags = [str(tag) for tag in (customer.get("tags") or [])]
        lowered_tags = {tag.strip().lower() for tag in tags}
        if "super-admin" in lowered_tags:
            platform_admin_ids.add(cid)
            platform_admins.append(_admin_identity(customer))
        member_handles: set[str] = set()
        admin_handles: set[str] = set()
        for tag in tags:
            admin_handle = _tag_handle(tag, ("storefront-admin--", "b2b-admin-"), handles)
            member_handle = _tag_handle(tag, ("storefront-member--",), handles)
            legacy_member = ""
            lowered = tag.strip().lower()
            if lowered.startswith("b2b-") and not lowered.startswith("b2b-admin-"):
                candidate = lowered[len("b2b-"):]
                if candidate in handles:
                    legacy_member = candidate
            if admin_handle:
                admin_handles.add(admin_handle)
                member_handles.add(admin_handle)
            if member_handle:
                member_handles.add(member_handle)
            if legacy_member:
                member_handles.add(legacy_member)

        for handle in member_handles:
            by_store[handle]["member_ids"].add(cid)
            unique_members.add(cid)
        for handle in admin_handles:
            by_store[handle]["admin_ids"].add(cid)
            by_store[handle]["admins"].append(_admin_identity(customer))

    result: Dict[str, Any] = {}
    unique_shopper_ids: set[str] = set()
    platform_admins = sorted(platform_admins, key=lambda row: (row.get("display_name") or "", row.get("email") or ""))
    for handle, data in by_store.items():
        admins = sorted(data["admins"], key=lambda row: (row.get("display_name") or "", row.get("email") or ""))
        shopper_ids = data["member_ids"].difference(data["admin_ids"]).difference(platform_admin_ids)
        unique_shopper_ids.update(shopper_ids)
        result[handle] = {
            "total": len(data["member_ids"]),
            "member_count": len(data["member_ids"]),
            "shopper_count": len(shopper_ids),
            "store_admin_count": len(data["admin_ids"]),
            "store_admins": admins,
            "platform_admin_count": len(platform_admin_ids),
            "platform_admins": platform_admins,
            "effective_admin_count": len(data["admin_ids"].union(platform_admin_ids)),
        }
    return result, len(unique_members), {
        "count": len(platform_admin_ids),
        "admins": platform_admins,
        "unique_shopper_count": len(unique_shopper_ids),
    }


def _aggregate_products(products: list[Dict[str, Any]], handles: set[str]) -> Dict[str, Any]:
    result = {
        handle: {
            "total": 0,
            "live": 0,
            "draft": 0,
            "archived": 0,
            "newest_product": None,
            "last_product_update": None,
        }
        for handle in handles
    }
    for product in products:
        product_handles = handles.intersection({str(tag).strip().lower() for tag in (product.get("tags") or [])})
        for handle in product_handles:
            result[handle]["total"] += 1
            status = str(product.get("status") or "").upper()
            if status == "ACTIVE":
                result[handle]["live"] += 1
            elif status == "ARCHIVED":
                result[handle]["archived"] += 1
            else:
                result[handle]["draft"] += 1
            product_summary = {
                "id": str(product.get("id") or ""),
                "title": str(product.get("title") or "Untitled product"),
                "handle": str(product.get("handle") or ""),
                "status": status or "UNKNOWN",
                "created_at": product.get("createdAt"),
                "updated_at": product.get("updatedAt"),
            }
            newest = result[handle].get("newest_product") or {}
            if str(product_summary.get("created_at") or "") > str(newest.get("created_at") or ""):
                result[handle]["newest_product"] = product_summary
            latest = result[handle].get("last_product_update") or {}
            if str(product_summary.get("updated_at") or "") > str(latest.get("updated_at") or ""):
                result[handle]["last_product_update"] = product_summary
    return result


def _aggregate_orders(
    orders: list[Dict[str, Any]],
    handles: set[str],
) -> tuple[Dict[str, Any], int, int, bool, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {
        handle: {
            "gross": Decimal("0"),
            "currency": "USD",
            "order_ids": set(),
            "last_purchase": None,
            "recent_purchases": [],
        }
        for handle in handles
    }
    unattributed = 0
    ambiguous = 0
    nested_truncated = False
    attributed_order_ids: set[str] = set()
    latest_purchase: Optional[Dict[str, Any]] = None
    # PARTIALLY_PAID is intentionally excluded: the Command Center labels this
    # metric as paid orders, so an outstanding balance must not count as a sale.
    allowed_statuses = {"PAID", "PARTIALLY_REFUNDED", "REFUNDED"}
    for order in orders:
        if order.get("cancelledAt") or str(order.get("displayFinancialStatus") or "").upper() not in allowed_statuses:
            continue
        order_amounts: Dict[str, Decimal] = {}
        line_items = order.get("lineItems") or {}
        if (line_items.get("pageInfo") or {}).get("hasNextPage"):
            nested_truncated = True
        for line in line_items.get("nodes") or []:
            product = line.get("product") or {}
            matching = handles.intersection({str(tag).strip().lower() for tag in (product.get("tags") or [])})
            if not matching:
                unattributed += 1
                continue
            if len(matching) > 1:
                # A product is expected to belong to one private storefront.
                # Choose deterministically instead of double-counting revenue,
                # and surface the tagging problem in data_quality.
                ambiguous += 1
            handle = sorted(matching)[0]
            money = (((line.get("originalTotalSet") or {}).get("shopMoney")) or {})
            amount = _decimal(money.get("amount"))
            currency = str(money.get("currencyCode") or "USD")
            result[handle]["gross"] += amount
            result[handle]["currency"] = currency
            result[handle]["order_ids"].add(str(order.get("id") or order.get("name") or ""))
            order_amounts[handle] = order_amounts.get(handle, Decimal("0")) + amount

        for handle, amount in order_amounts.items():
            order_id = str(order.get("id") or order.get("name") or "")
            if order_id:
                attributed_order_ids.add(order_id)
            purchase = {
                "event_type": "purchase",
                "store_handle": handle,
                "order_name": str(order.get("name") or ""),
                "created_at": order.get("createdAt"),
                "at": order.get("createdAt"),
                "customer_display_name": str((order.get("customer") or {}).get("displayName") or "Guest").strip(),
                "gross_amount": _money_string(amount),
                "currency": result[handle]["currency"],
                "financial_status": order.get("displayFinancialStatus"),
                "fulfillment_status": order.get("displayFulfillmentStatus"),
            }
            if result[handle]["last_purchase"] is None:
                result[handle]["last_purchase"] = purchase
            if latest_purchase is None or str(purchase.get("created_at") or "") > str(latest_purchase.get("created_at") or ""):
                latest_purchase = purchase
            if len(result[handle]["recent_purchases"]) < 10:
                result[handle]["recent_purchases"].append(purchase)

    normalized: Dict[str, Any] = {}
    for handle, data in result.items():
        normalized[handle] = {
            "gross_sales": _money_string(data["gross"]),
            "currency": data["currency"],
            "order_count": len(data["order_ids"]),
            "last_purchase": data["last_purchase"],
            "recent_purchases": data["recent_purchases"],
        }
    return normalized, unattributed, ambiguous, nested_truncated, {
        "paid_order_count": len(attributed_order_ids),
        "latest_purchase": latest_purchase,
    }


def _store_decision(store: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Classify traction without ever taking an automatic store action."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    outreach = store.get("outreach") or {}
    delete_due = _parse_datetime(outreach.get("delete_due_at"))
    outreach_delete_eligible = bool(
        delete_due
        and delete_due <= current
        and str(outreach.get("claim_status") or "unclaimed").strip().lower() not in {"claimed", "active"}
    )
    age_days = _age_days(store.get("created_at"), now=current)
    activity = store.get("activity") or {}
    customers = store.get("customers") or {}
    products = store.get("products") or {}
    sales = store.get("sales") or {}
    observable = store.get("observability") or {}
    sales_known = bool(observable.get("sales", True))
    customers_known = bool(observable.get("customers", True))
    sessions_known = bool(observable.get("sessions", True))
    products_known = bool(observable.get("products", True))
    tracking_age_days = _age_days(activity.get("tracking_started_at"), now=current)
    paid_orders = int(sales.get("order_count") or 0)
    shopper_count = int(customers.get("shopper_count") or 0)
    non_founder_sessions = int(activity.get("non_super_admin_sessions") or 0)
    product_count = int(products.get("total") or 0)
    source = str(store.get("source") or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    is_cold_outreach = source in {"cold_outreach", "outreach", "cold_email", "prospecting"}

    reasons: list[str] = []
    if not sales_known:
        reasons.append("Sales are not observable")
    if not customers_known:
        reasons.append("Member data is not observable")
    if not sessions_known:
        reasons.append("Session data is not observable")
    if not products_known:
        reasons.append("Product data is not observable")
    if products_known and product_count == 0:
        reasons.append("No products are attributed to this store")
    if sessions_known and activity.get("tracking_started_at") is None:
        reasons.append("Storefront sessions are not instrumented yet")
    if customers_known and age_days is not None and age_days >= 7 and shopper_count == 0:
        reasons.append("No non-admin members after 7 days")
    if sales_known and age_days is not None and age_days >= 7 and paid_orders == 0:
        reasons.append("No paid orders after 7 days")
    if sessions_known and tracking_age_days is not None and tracking_age_days >= 7 and non_founder_sessions == 0:
        reasons.append("No non-founder sessions during 7 tracked days")

    observation_complete = (
        age_days is not None
        and age_days >= 7
        and tracking_age_days is not None
        and tracking_age_days >= 7
        and sales_known
        and customers_known
        and sessions_known
    )
    no_traction = paid_orders == 0 and shopper_count == 0 and non_founder_sessions == 0
    review_candidate = observation_complete and no_traction
    delete_candidate = review_candidate and is_cold_outreach

    if not (sales_known and customers_known and sessions_known):
        traction = "not_observable"
    elif paid_orders > 0:
        traction = "converting"
    elif shopper_count > 0 or non_founder_sessions > 0:
        traction = "engaged"
    elif age_days is not None and age_days < 7:
        traction = "new"
    elif activity.get("tracking_started_at") is None:
        traction = "not_tracked"
    elif review_candidate:
        traction = "no_traction"
    else:
        traction = "watch"

    if review_candidate and not is_cold_outreach:
        reasons.append("Confirm this is a cold-outreach store before deleting")
    if outreach_delete_eligible:
        reasons.append("Outreach retention window reached; review the deletion queue")

    return {
        "traction": traction,
        "age_days": age_days,
        "tracking_age_days": tracking_age_days,
        "needs_attention": bool(reasons),
        "reasons": reasons,
        "review_candidate": review_candidate,
        "delete_candidate": delete_candidate,
        "outreach_delete_eligible": outreach_delete_eligible,
        "delete_after_at": outreach.get("delete_due_at"),
        "automatic_action": False,
    }


def _cache_get() -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        if not _SUMMARY_CACHE:
            return None
        saved_at, payload = _SUMMARY_CACHE
        if time.monotonic() - saved_at > _SUMMARY_CACHE_TTL:
            return None
        return json.loads(json.dumps(payload))


def _cache_set(payload: Dict[str, Any]) -> None:
    global _SUMMARY_CACHE
    with _CACHE_LOCK:
        _SUMMARY_CACHE = (time.monotonic(), json.loads(json.dumps(payload)))


def _build_summary(core) -> Dict[str, Any]:
    store_nodes = _store_nodes(core)
    outreach_states: Dict[str, Dict[str, Any]] = {}
    try:
        outreach_states = outreach_tracking.list_all(core)
    except Exception as exc:
        outreach_states = {}
    stores: list[Dict[str, Any]] = []
    for node in store_nodes:
        handle = _normalized_handle(str(node.get("handle") or ""))
        if not handle:
            continue
        fields = _field_map(node)
        source = next((fields.get(key) for key in (
            "source",
            "store_source",
            "request_source",
            "acquisition_source",
            "origin",
        ) if fields.get(key)), "unknown")
        stores.append({
            "handle": handle,
            "name": fields.get("name") or str(node.get("displayName") or handle),
            "collection_handle": fields.get("collection_handle") or handle,
            "collection_gid": fields.get("collection_gid") or "",
            "status": (fields.get("status") or "active").strip().lower(),
            "join_mode": (fields.get("join_mode") or "secure").strip().lower(),
            "source": str(source).strip().lower() or "unknown",
            "created_at": fields.get("created_at") or fields.get("createdAt") or None,
            "updated_at": fields.get("updated_at") or fields.get("updatedAt") or None,
        })
        outreach = outreach_states.get(handle)
        if outreach:
            store = stores[-1]
            store["outreach"] = dict(outreach)
            # Repository outreach timestamps are the canonical order for new
            # stores; legacy stores continue to use their collection timestamp.
            store["created_at"] = outreach.get("built_at") or outreach.get("created_at") or store.get("created_at")
            store["build_timestamp"] = outreach.get("built_at") or outreach.get("created_at")
            now = datetime.now(timezone.utc)
            delete_due = _parse_datetime(outreach.get("delete_due_at"))
            followup_due = _parse_datetime(outreach.get("followup_due_at"))
            claimed = str(outreach.get("claim_status") or "unclaimed").strip().lower() in {"claimed", "active"}
            store["outreach"]["delete_eligible"] = bool(delete_due and delete_due <= now and not claimed)
            store["outreach"]["followup_due"] = bool(
                followup_due and followup_due <= now and not outreach.get("followup_sent_at")
            )
    handles = {store["handle"] for store in stores}

    scopes: set[str] = set()
    data_quality: Dict[str, Any] = {}
    try:
        collection_dates = _collection_dates(core, (store.get("collection_gid") for store in stores))
        for store in stores:
            dates = collection_dates.get(str(store.get("collection_gid") or "")) or {}
            store["created_at"] = store.get("created_at") or dates.get("created_at") or None
            store["updated_at"] = store.get("updated_at") or dates.get("updated_at") or None
    except Exception as exc:
        data_quality["store_age"] = "Not observable: " + str(exc)[:180]
    try:
        scopes = _access_scopes(core)
    except Exception as exc:
        data_quality["access_scopes"] = "Not observable: " + str(exc)[:180]

    customers: list[Dict[str, Any]] = []
    try:
        customers = _customer_nodes(core)
    except Exception as exc:
        data_quality["customers"] = "Not observable: " + str(exc)[:180]
    customer_stats, unique_customers, platform_admins = _aggregate_customers(customers, handles)

    products: list[Dict[str, Any]] = []
    try:
        products = _product_nodes(core)
    except Exception as exc:
        data_quality["products"] = "Not observable: " + str(exc)[:180]
    product_stats = _aggregate_products(products, handles)

    orders: list[Dict[str, Any]] = []
    order_cap_truncated = False
    if "read_orders" in scopes or not scopes:
        try:
            orders, order_cap_truncated = _order_nodes(core)
        except Exception as exc:
            data_quality["sales"] = "Not observable: " + str(exc)[:180]
    else:
        data_quality["sales"] = "Not observable: read_orders scope is missing"
    order_stats, unattributed_lines, ambiguous_lines, nested_truncated, global_order_stats = _aggregate_orders(orders, handles)

    has_all_orders = "read_all_orders" in scopes
    if "sales" in data_quality:
        sales_coverage = "unavailable"
    elif has_all_orders and order_cap_truncated:
        sales_coverage = f"first_{_MAX_ORDERS}_orders"
    elif has_all_orders:
        sales_coverage = "lifetime"
    else:
        sales_coverage = "last_60_days"
    if nested_truncated:
        data_quality["sales_line_items"] = "Some orders contained more than 10 line items"
    if unattributed_lines:
        data_quality["unattributed_order_lines"] = unattributed_lines
    if ambiguous_lines:
        data_quality["ambiguous_order_lines"] = ambiguous_lines

    try:
        activities = _activity_nodes(core)
    except Exception as exc:
        activities = {}
        data_quality["sessions"] = "Not observable: " + str(exc)[:180]
    total_gross = Decimal("0")
    total_sessions = 0
    non_super_sessions = 0
    admin_memberships = 0
    member_memberships = 0
    currency = "USD"
    for store in stores:
        handle = store["handle"]
        members = customer_stats.get(handle) or {
            "total": 0,
            "member_count": 0,
            "shopper_count": 0,
            "store_admin_count": 0,
            "store_admins": [],
            "platform_admin_count": platform_admins["count"],
            "platform_admins": platform_admins["admins"],
            "effective_admin_count": platform_admins["count"],
        }
        product = product_stats.get(handle) or {
            "total": 0,
            "live": 0,
            "draft": 0,
            "archived": 0,
            "newest_product": None,
            "last_product_update": None,
        }
        sales = order_stats.get(handle) or {"gross_sales": "0.00", "currency": "USD", "order_count": 0, "last_purchase": None}
        activity = activities.get(handle) or {}
        sales["coverage"] = sales_coverage
        sales["is_lifetime"] = sales_coverage == "lifetime"
        sales["observable"] = sales_coverage != "unavailable"
        store["customers"] = members
        store["products"] = product
        store["sales"] = sales
        store["activity"] = {
            "tracking_started_at": activity.get("tracking_started_at"),
            "total_sessions": int(activity.get("total_sessions") or 0),
            "non_super_admin_sessions": int(activity.get("non_super_admin_sessions") or 0),
            "last_session": activity.get("last_session"),
            "last_non_super_admin_session": activity.get("last_non_super_admin_session"),
            "last_authenticated_customer_activity": activity.get("last_authenticated_customer_activity"),
            "recent_sessions": activity.get("recent_activity") or [],
            "source": "first_party_storefront_tracker",
        }
        store["recent_activity"] = sorted(
            list(store["activity"]["recent_sessions"]) + list(sales.get("recent_purchases") or []),
            key=lambda row: str(row.get("at") or row.get("created_at") or ""),
            reverse=True,
        )[:25]
        store["observability"] = {
            "sales": sales_coverage != "unavailable",
            "customers": "customers" not in data_quality,
            "products": "products" not in data_quality,
            "sessions": "sessions" not in data_quality,
            "store_age": "store_age" not in data_quality and bool(store.get("created_at")),
        }
        store["decision"] = _store_decision(store)
        total_gross += _decimal(sales.get("gross_sales"))
        currency = str(sales.get("currency") or currency)
        total_sessions += store["activity"]["total_sessions"]
        non_super_sessions += store["activity"]["non_super_admin_sessions"]
        member_memberships += int(members.get("total") or 0)
        admin_memberships += int(members.get("store_admin_count") or 0)

    newest_store = max(
        (store for store in stores if store.get("created_at")),
        key=lambda row: str(row.get("created_at") or ""),
        default=None,
    )
    newest_product: Optional[Dict[str, Any]] = None
    latest_non_super_session: Optional[Dict[str, Any]] = None
    global_recent_activity: list[Dict[str, Any]] = []
    for store in stores:
        product = (store.get("products") or {}).get("newest_product")
        if product and (
            newest_product is None
            or str(product.get("created_at") or "") > str(newest_product.get("created_at") or "")
        ):
            newest_product = dict(product)
            newest_product["store_handle"] = store["handle"]
            newest_product["store_name"] = store["name"]
        session = (store.get("activity") or {}).get("last_non_super_admin_session")
        if session and (
            latest_non_super_session is None
            or str(session.get("at") or "") > str(latest_non_super_session.get("at") or "")
        ):
            latest_non_super_session = dict(session)
            latest_non_super_session["store_handle"] = store["handle"]
            latest_non_super_session["store_name"] = store["name"]
        for event in store.get("recent_activity") or []:
            enriched = dict(event)
            enriched.setdefault("store_handle", store["handle"])
            enriched.setdefault("store_name", store["name"])
            global_recent_activity.append(enriched)

    stores.sort(key=lambda row: (row.get("name") or row["handle"]).lower())
    stores.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    decisions = [store.get("decision") or {} for store in stores]
    deletion_queue = [
        {
            "handle": store["handle"],
            "name": store["name"],
            "delete_due_at": (store.get("outreach") or {}).get("delete_due_at"),
            "claim_status": (store.get("outreach") or {}).get("claim_status") or "unclaimed",
            "status": (store.get("outreach") or {}).get("status") or store.get("status"),
            "reason": "7-day cold-outreach retention window reached; review before explicit deletion",
        }
        for store in stores
        if (store.get("outreach") or {}).get("delete_eligible")
    ]
    followups_due = sum(1 for store in stores if (store.get("outreach") or {}).get("followup_due"))
    highlights = {
        "newest_store": ({
            "handle": newest_store["handle"],
            "name": newest_store["name"],
            "created_at": newest_store.get("created_at"),
            "age_days": (newest_store.get("decision") or {}).get("age_days"),
            "build_timestamp": newest_store.get("build_timestamp"),
        } if newest_store else None),
        "newest_product": newest_product,
        "latest_purchase": global_order_stats.get("latest_purchase"),
        "latest_non_super_admin_session": latest_non_super_session,
        "deletion_queue": deletion_queue,
        "followups_due": followups_due,
    }

    return {
        "ok": True,
        "generated_at": _utc_now(),
        "totals": {
            "stores": len(stores),
            "gross_sales": _money_string(total_gross),
            "currency": currency,
            "sales_coverage": sales_coverage,
            "sales_observable": sales_coverage != "unavailable",
            "paid_orders": int(global_order_stats.get("paid_order_count") or 0),
            "tracked_sessions": total_sessions,
            "sessions_observable": "sessions" not in data_quality,
            "non_super_admin_sessions": non_super_sessions,
            "unique_tagged_customers": unique_customers,
            "unique_shoppers": int(platform_admins.get("unique_shopper_count") or 0),
            "customers_observable": "customers" not in data_quality,
            "customer_memberships": member_memberships,
            "store_admin_memberships": admin_memberships,
            "platform_admins": platform_admins["count"],
            "products": sum(int((store.get("products") or {}).get("total") or 0) for store in stores),
            "products_observable": "products" not in data_quality,
            "needs_attention": sum(1 for decision in decisions if decision.get("needs_attention")),
            "review_candidates": sum(1 for decision in decisions if decision.get("review_candidate")),
            "delete_candidates": sum(1 for decision in decisions if decision.get("delete_candidate")),
            "outreach_stores": sum(1 for store in stores if store.get("outreach")),
            "followups_due": followups_due,
            "outreach_delete_queue": len(deletion_queue),
        },
        "highlights": highlights,
        "recent_activity": sorted(
            global_recent_activity,
            key=lambda row: str(row.get("at") or row.get("created_at") or ""),
            reverse=True,
        )[:50],
        "stores": stores,
        "data_quality": data_quality,
        "definitions": {
            "gross_sales": "Paid product line totals before discounts and refunds, attributed by the store-handle product tag.",
            "sessions": "First-party storefront sessions recorded after activity tracking was deployed.",
            "last_non_super_admin_session": "Latest session not linked to the super-admin tag; anonymous sessions cannot prove a person's identity.",
            "role_unknown": "A signed-in session whose Shopify tags could not be read; excluded from non-super-admin and last-customer metrics.",
            "effective_admin_count": "Unique store-scoped admins plus platform super-admins who can manage the store.",
            "shopper_count": "Tagged store members excluding store-scoped and platform administrators.",
            "store_age": "Age of the Shopify collection created during store provisioning; no live store record is modified.",
            "review_candidate": "At least 7 days old, at least 7 tracked days, and no paid orders, non-admin members, or non-founder sessions. Review only; never automatically deleted.",
        },
    }


def install_command_center_routes(app, core) -> None:
    @app.post("/api/store/{handle}/activity/session")
    async def command_center_track_session(handle: str, request: Request):
        denied = core._require_admin_secret(request)
        if denied is not None:
            return denied

        handle = _normalized_handle(handle)
        if not handle:
            return JSONResponse({"ok": False, "error": "invalid store handle"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)

        session_id = str((body or {}).get("session_id") or "").strip()
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            return JSONResponse({"ok": False, "error": "invalid session id"}, status_code=400)
        path = str((body or {}).get("path") or "")[:256]
        occurred_at = _utc_now()
        customer_id = _normalized_customer_id(request.headers.get("X-SS-Customer-Id", ""))
        is_super_admin = request.headers.get("X-SS-Superadmin", "").strip() == "1"
        role_known = request.headers.get("X-SS-Role-Unknown", "").strip() != "1"
        session_hash = hashlib.sha256(f"{handle}:{session_id}".encode("utf-8")).hexdigest()

        try:
            def record_session() -> Optional[tuple[Dict[str, Any], bool]]:
                display_name = "" if is_super_admin or not role_known else _customer_display_name(core, customer_id)
                with _ACTIVITY_LOCK:
                    if not _store_exists(core, handle):
                        return None
                    _ensure_activity_definition(core)
                    state = _activity_state(core, handle)
                    updated_state, is_duplicate = _apply_activity_event(
                        state,
                        session_hash=session_hash,
                        occurred_at=occurred_at,
                        path=path,
                        customer_id=customer_id,
                        customer_display_name=display_name,
                        is_super_admin=is_super_admin,
                        role_known=role_known,
                    )
                    if not is_duplicate:
                        _save_activity_state(core, handle, updated_state)
                    return updated_state, is_duplicate

            # All Shopify Admin API calls run in a worker thread. Monitoring
            # traffic must never occupy the async event loop used by live
            # storefront and provisioning requests.
            recorded = await run_in_threadpool(record_session)
            if recorded is None:
                return JSONResponse({"ok": False, "error": "store not found"}, status_code=404)
            updated, duplicate = recorded
            return {
                "ok": True,
                "duplicate": duplicate,
                "total_sessions": int(updated.get("total_sessions") or 0),
                "non_super_admin_sessions": int(updated.get("non_super_admin_sessions") or 0),
            }
        except Exception:
            core.log.exception("Command Center activity tracking failed handle=%s", handle)
            return JSONResponse({"ok": False, "error": "activity tracking unavailable"}, status_code=502)

    @app.get("/admin/command-center/summary")
    def command_center_summary(request: Request, refresh: bool = False):
        denied = core._require_admin_secret(request)
        if denied is not None:
            return denied
        if request.headers.get("X-SS-Superadmin", "").strip() != "1":
            return JSONResponse({"ok": False, "error": "super-admin access required"}, status_code=403)
        if not refresh:
            cached = _cache_get()
            if cached is not None:
                cached["cached"] = True
                return cached
        try:
            with _SUMMARY_BUILD_LOCK:
                # A second request may have waited while another request built
                # the snapshot. Reuse it instead of doubling Shopify reads.
                if not refresh:
                    cached = _cache_get()
                    if cached is not None:
                        cached["cached"] = True
                        return cached
                payload = _build_summary(core)
                payload["cached"] = False
                _cache_set(payload)
                return payload
        except Exception:
            core.log.exception("Command Center summary failed")
            return JSONResponse({"ok": False, "error": "command center summary unavailable"}, status_code=502)
