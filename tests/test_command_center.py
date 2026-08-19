from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from command_center import (  # noqa: E402
    _aggregate_customers,
    _aggregate_orders,
    _aggregate_products,
    _apply_activity_event,
    _build_summary,
    _store_decision,
)


def test_activity_counts_each_session_once_and_excludes_platform_admin_from_other_activity():
    first, duplicate = _apply_activity_event(
        {},
        session_hash="customer-session",
        occurred_at="2026-08-19T12:00:00+00:00",
        path="/collections/team-one",
        customer_id="123",
        customer_display_name="A Customer",
        is_super_admin=False,
    )
    assert duplicate is False
    assert first["total_sessions"] == 1
    assert first["non_super_admin_sessions"] == 1
    assert first["last_authenticated_customer_activity"]["customer_display_name"] == "A Customer"
    assert first["recent_activity"] == [{
        "event_type": "session",
        "at": "2026-08-19T12:00:00+00:00",
        "path": "/collections/team-one",
        "visitor_type": "customer",
        "customer_display_name": "A Customer",
    }]

    unchanged, duplicate = _apply_activity_event(
        first,
        session_hash="customer-session",
        occurred_at="2026-08-19T12:05:00+00:00",
        path="/collections/team-one/products/a-shirt",
        customer_id="123",
        customer_display_name="A Customer",
        is_super_admin=False,
    )
    assert duplicate is True
    assert unchanged["total_sessions"] == 1

    final, duplicate = _apply_activity_event(
        unchanged,
        session_hash="founder-session",
        occurred_at="2026-08-19T12:10:00+00:00",
        path="/collections/team-one",
        customer_id="999",
        customer_display_name="Founder",
        is_super_admin=True,
    )
    assert duplicate is False
    assert final["total_sessions"] == 2
    assert final["non_super_admin_sessions"] == 1
    assert final["last_session"]["visitor_type"] == "super_admin"
    assert final["last_non_super_admin_session"]["at"] == "2026-08-19T12:00:00+00:00"
    assert len(final["recent_activity"]) == 2
    assert final["recent_activity"][-1]["visitor_type"] == "super_admin"

    unknown, duplicate = _apply_activity_event(
        final,
        session_hash="unknown-role-session",
        occurred_at="2026-08-19T12:15:00+00:00",
        path="/collections/team-one",
        customer_id="999",
        customer_display_name="",
        is_super_admin=False,
        role_known=False,
    )
    assert duplicate is False
    assert unknown["total_sessions"] == 3
    assert unknown["non_super_admin_sessions"] == 1
    assert unknown["last_session"]["visitor_type"] == "role_unknown"
    assert unknown["last_non_super_admin_session"]["at"] == "2026-08-19T12:00:00+00:00"


def test_customer_rollup_separates_store_admins_and_platform_super_admins():
    customers = [
        {
            "id": "gid://shopify/Customer/1",
            "displayName": "Store Owner",
            "email": "owner@example.com",
            "tags": ["storefront-admin--team-one"],
        },
        {
            "id": "gid://shopify/Customer/2",
            "displayName": "Legacy Member",
            "email": "member@example.com",
            "tags": ["b2b-team-one"],
        },
        {
            "id": "gid://shopify/Customer/3",
            "displayName": "Platform Owner",
            "email": "platform@example.com",
            "tags": ["super-admin"],
        },
        {
            "id": "gid://shopify/Customer/4",
            "displayName": "Other Admin",
            "email": "other@example.com",
            "tags": ["b2b-admin-team-two"],
        },
    ]

    stores, unique_members, platform = _aggregate_customers(customers, {"team-one", "team-two"})

    assert unique_members == 3
    assert platform["count"] == 1
    assert platform["admins"][0]["display_name"] == "Platform Owner"
    assert stores["team-one"]["total"] == 2
    assert stores["team-one"]["shopper_count"] == 1
    assert stores["team-one"]["store_admin_count"] == 1
    assert stores["team-one"]["platform_admin_count"] == 1
    assert stores["team-one"]["effective_admin_count"] == 2
    assert stores["team-two"]["total"] == 1
    assert stores["team-two"]["shopper_count"] == 0
    assert stores["team-two"]["effective_admin_count"] == 2
    assert platform["unique_shopper_count"] == 1


def test_paid_order_rollup_attributes_by_product_tag_without_double_counting():
    orders = [
        {
            "id": "order-2",
            "name": "#1002",
            "createdAt": "2026-08-19T12:00:00Z",
            "cancelledAt": None,
            "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "UNFULFILLED",
            "customer": {"displayName": "Newest Customer"},
            "lineItems": {
                "nodes": [
                    {
                        "originalTotalSet": {"shopMoney": {"amount": "30.00", "currencyCode": "USD"}},
                        "product": {"tags": ["team-one"]},
                    },
                    {
                        "originalTotalSet": {"shopMoney": {"amount": "5.00", "currencyCode": "USD"}},
                        "product": {"tags": ["unrelated"]},
                    },
                ],
                "pageInfo": {"hasNextPage": False},
            },
        },
        {
            "id": "order-1",
            "name": "#1001",
            "createdAt": "2026-08-18T12:00:00Z",
            "cancelledAt": None,
            "displayFinancialStatus": "PARTIALLY_REFUNDED",
            "displayFulfillmentStatus": "FULFILLED",
            "customer": {"displayName": "Earlier Customer"},
            "lineItems": {
                "nodes": [
                    {
                        "originalTotalSet": {"shopMoney": {"amount": "20.00", "currencyCode": "USD"}},
                        "product": {"tags": ["team-one", "team-two"]},
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            },
        },
        {
            "id": "cancelled",
            "name": "#1000",
            "createdAt": "2026-08-17T12:00:00Z",
            "cancelledAt": "2026-08-17T13:00:00Z",
            "displayFinancialStatus": "PAID",
            "lineItems": {
                "nodes": [
                    {
                        "originalTotalSet": {"shopMoney": {"amount": "999.00", "currencyCode": "USD"}},
                        "product": {"tags": ["team-one"]},
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            },
        },
    ]

    stores, unattributed, ambiguous, nested_truncated, global_stats = _aggregate_orders(
        orders, {"team-one", "team-two"}
    )

    assert Decimal(stores["team-one"]["gross_sales"]) == Decimal("50.00")
    assert stores["team-one"]["order_count"] == 2
    assert stores["team-one"]["last_purchase"]["order_name"] == "#1002"
    assert [row["order_name"] for row in stores["team-one"]["recent_purchases"]] == ["#1002", "#1001"]
    assert stores["team-two"]["gross_sales"] == "0.00"
    assert unattributed == 1
    assert ambiguous == 1
    assert nested_truncated is False
    assert global_stats["paid_order_count"] == 2
    assert global_stats["latest_purchase"]["order_name"] == "#1002"


def test_partially_paid_order_is_not_reported_as_paid_sale():
    stores, *_rest, global_stats = _aggregate_orders([
        {
            "id": "deposit-only",
            "name": "#1003",
            "createdAt": "2026-08-19T13:00:00Z",
            "cancelledAt": None,
            "displayFinancialStatus": "PARTIALLY_PAID",
            "lineItems": {
                "nodes": [{
                    "originalTotalSet": {"shopMoney": {"amount": "100.00", "currencyCode": "USD"}},
                    "product": {"tags": ["team-one"]},
                }],
                "pageInfo": {"hasNextPage": False},
            },
        }
    ], {"team-one"})

    assert stores["team-one"]["order_count"] == 0
    assert stores["team-one"]["gross_sales"] == "0.00"
    assert global_stats["paid_order_count"] == 0


def test_product_rollup_exposes_newest_build_and_latest_update():
    stores = _aggregate_products([
        {
            "id": "p-old",
            "title": "Original Tee",
            "handle": "original-tee",
            "status": "ACTIVE",
            "tags": ["team-one"],
            "createdAt": "2026-08-01T12:00:00Z",
            "updatedAt": "2026-08-19T12:00:00Z",
        },
        {
            "id": "p-new",
            "title": "New Hoodie",
            "handle": "new-hoodie",
            "status": "DRAFT",
            "tags": ["team-one"],
            "createdAt": "2026-08-18T12:00:00Z",
            "updatedAt": "2026-08-18T12:00:00Z",
        },
    ], {"team-one"})

    assert stores["team-one"]["total"] == 2
    assert stores["team-one"]["live"] == 1
    assert stores["team-one"]["draft"] == 1
    assert stores["team-one"]["newest_product"]["title"] == "New Hoodie"
    assert stores["team-one"]["last_product_update"]["title"] == "Original Tee"


def test_delete_review_requires_seven_real_tracking_days_and_never_acts_automatically():
    base = {
        "created_at": "2026-08-01T12:00:00Z",
        "source": "cold_outreach",
        "sales": {"order_count": 0},
        "customers": {"shopper_count": 0},
        "products": {"total": 7},
        "activity": {
            "tracking_started_at": "2026-08-18T12:00:00Z",
            "non_super_admin_sessions": 0,
        },
    }
    now = datetime.fromisoformat("2026-08-19T12:00:00+00:00")
    too_soon = _store_decision(base, now=now)
    assert too_soon["review_candidate"] is False
    assert too_soon["delete_candidate"] is False
    assert too_soon["automatic_action"] is False

    base["activity"]["tracking_started_at"] = "2026-08-10T12:00:00Z"
    observed = _store_decision(base, now=now)
    assert observed["review_candidate"] is True
    assert observed["delete_candidate"] is True
    assert observed["automatic_action"] is False


def test_unknown_source_is_review_only_even_after_no_traction_window():
    store = {
        "created_at": "2026-08-01T12:00:00Z",
        "source": "unknown",
        "sales": {"order_count": 0},
        "customers": {"shopper_count": 0},
        "products": {"total": 7},
        "activity": {
            "tracking_started_at": "2026-08-01T12:00:00Z",
            "non_super_admin_sessions": 0,
        },
    }
    now = datetime.fromisoformat("2026-08-19T12:00:00+00:00")
    decision = _store_decision(store, now=now)
    assert decision["review_candidate"] is True
    assert decision["delete_candidate"] is False
    assert "Confirm this is a cold-outreach store before deleting" in decision["reasons"]


def test_missing_monitoring_source_can_never_create_a_delete_candidate():
    store = {
        "created_at": "2026-08-01T12:00:00Z",
        "source": "cold_outreach",
        "observability": {"sales": False, "customers": True, "sessions": True, "products": True},
        "sales": {"order_count": 0},
        "customers": {"shopper_count": 0},
        "products": {"total": 7},
        "activity": {
            "tracking_started_at": "2026-08-01T12:00:00Z",
            "non_super_admin_sessions": 0,
        },
    }
    now = datetime.fromisoformat("2026-08-19T12:00:00+00:00")
    decision = _store_decision(store, now=now)
    assert decision["traction"] == "not_observable"
    assert decision["review_candidate"] is False
    assert decision["delete_candidate"] is False


def test_summary_is_query_only_and_exposes_decision_cockpit_totals():
    class Core:
        def __init__(self):
            self.queries = []

        def _shopify_graphql(self, query, variables):
            self.queries.append(query)
            assert "mutation" not in query.lower()
            if "CommandCenterStores" in query:
                return {
                    "metaobjects": {
                        "nodes": [{
                            "handle": "team-one",
                            "displayName": "Team One",
                            "fields": [
                                {"key": "name", "value": "Team One"},
                                {"key": "collection_gid", "value": "gid://shopify/Collection/1"},
                                {"key": "collection_handle", "value": "team-one"},
                                {"key": "source", "value": "cold_outreach"},
                                {"key": "status", "value": "active"},
                            ],
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            if "CommandCenterCollectionDates" in query:
                return {"nodes": [{
                    "id": "gid://shopify/Collection/1",
                    "createdAt": "2026-08-01T12:00:00Z",
                    "updatedAt": "2026-08-18T12:00:00Z",
                }]}
            if "CommandCenterScopes" in query:
                return {"currentAppInstallation": {"accessScopes": [
                    {"handle": "read_orders"}, {"handle": "read_all_orders"},
                ]}}
            if "CommandCenterCustomers" in query:
                return {"customers": {
                    "nodes": [{
                        "id": "gid://shopify/Customer/1",
                        "displayName": "Member",
                        "email": "member@example.com",
                        "tags": ["storefront-member--team-one"],
                    }],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}
            if "CommandCenterProducts" in query:
                return {"products": {
                    "nodes": [{
                        "id": "gid://shopify/Product/1",
                        "title": "Team Tee",
                        "handle": "team-tee",
                        "status": "ACTIVE",
                        "tags": ["team-one"],
                        "createdAt": "2026-08-18T12:00:00Z",
                        "updatedAt": "2026-08-18T12:00:00Z",
                    }],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}
            if "CommandCenterOrders" in query:
                return {"orders": {
                    "nodes": [{
                        "id": "gid://shopify/Order/1",
                        "name": "#1001",
                        "createdAt": "2026-08-19T12:00:00Z",
                        "cancelledAt": None,
                        "displayFinancialStatus": "PAID",
                        "displayFulfillmentStatus": "UNFULFILLED",
                        "customer": {"displayName": "Member"},
                        "lineItems": {
                            "nodes": [{
                                "originalTotalSet": {"shopMoney": {"amount": "42.00", "currencyCode": "USD"}},
                                "product": {"tags": ["team-one"]},
                            }],
                            "pageInfo": {"hasNextPage": False},
                        },
                    }],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}
            if "CommandCenterActivities" in query:
                state = {
                    "tracking_started_at": "2026-08-10T12:00:00Z",
                    "total_sessions": 1,
                    "non_super_admin_sessions": 1,
                    "last_non_super_admin_session": {
                        "at": "2026-08-19T11:00:00Z",
                        "visitor_type": "customer",
                        "customer_display_name": "Member",
                    },
                    "recent_activity": [],
                }
                return {"metaobjects": {
                    "nodes": [{
                        "handle": "team-one",
                        "fields": [{"key": "data", "value": json.dumps(state)}],
                    }],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}
            raise AssertionError("Unexpected GraphQL query")

    core = Core()
    summary = _build_summary(core)

    assert summary["totals"]["stores"] == 1
    assert summary["totals"]["paid_orders"] == 1
    assert summary["totals"]["gross_sales"] == "42.00"
    assert summary["totals"]["unique_tagged_customers"] == 1
    assert summary["totals"]["unique_shoppers"] == 1
    assert summary["highlights"]["newest_store"]["handle"] == "team-one"
    assert summary["highlights"]["newest_product"]["title"] == "Team Tee"
    assert summary["highlights"]["latest_purchase"]["order_name"] == "#1001"
    assert summary["stores"][0]["decision"]["traction"] == "converting"
    assert all("mutation" not in query.lower() for query in core.queries)
