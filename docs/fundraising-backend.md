# Fundraising Backend — Stella & Sage

> **Branch:** `claude/dazzling-turing-76cjur`  
> **Payout cron:** OFF — no transfers are triggered automatically. No payout or transfer logic was changed in this document's scope.

---

## Overview

One singleton `store_fundraising` metaobject per store (keyed by store handle) holds the full fundraiser state as a JSON blob in a single `data` field. This matches the schemaless pattern used by `global_pricing`.

Fundraising routes live in `app.py`. All mutating routes require `X-Admin-Secret` (injected by the Printful\_Automation relay after it verifies the Shopify App Proxy signature and the caller's admin tag). The relay is the only authorized initiator for these routes.

---

## State fields

| Field | Type | Writable by | Notes |
|---|---|---|---|
| `enabled` | bool | body (`POST /api/fundraising/{handle}`) | true = fundraiser is live |
| `cause_name` | str | body | Display name for the cause |
| `amount` | int | body | Per-item donation amount in dollars |
| `goal` | float | body | Fundraising goal in dollars |
| `end_date` | str | body | ISO date string |
| `show_bar` | bool | body | Show progress bar on storefront |
| `setup_step` | str | body | Wizard step tracking |
| `stripe_account_id` | str | **server only** | Set by `fundraising_stripe_connect` |
| `stripe_connected` | bool | **server only** | Set by `fundraising_stripe_status` |
| `owner_customer_id` | str | **server only** | Numeric Shopify customer id of whoever launched the fundraiser |
| `owner_email` | str | **server only** | Email of the owner (best-effort, for display) |
| `total_raised` | float | **server only** | Updated by order-paid webhook |
| `ledger` | list | **server only** | Appended by order-paid webhook |
| `created_at` | str | **server only** | ISO timestamp of first launch |
| `updated_at` | str | **server only** | ISO timestamp of last write |
| `markup_add` | int | **server only** | Dollar markup currently applied to prices |
| `base_prices` | dict | **server only** | Snapshot of pre-fundraiser prices |
| `pricing_status` | str | **server only** | `pending`/`skipped`/`succeeded`/`failed` |
| `pricing_error` | str | **server only** | Error message if pricing failed |
| `pricing_updated_at` | str | **server only** | ISO timestamp of last pricing sync |
| `total_paid_out` | float | **server only** | Cumulative payout amount |
| `goal_met_notified` | bool | **server only** | Whether goal-met email was sent |

### Security note: `stripe_account_id` and `stripe_connected` are server-owned

`stripe_account_id` and `stripe_connected` are **not** in `_FR_ALLOWED_FIELDS`. A browser POST body that includes these fields will have them silently ignored. They can only be written by `POST /api/fundraising/{handle}/stripe/connect` and `GET /api/fundraising/{handle}/stripe/status`. This prevents a caller from spoofing Stripe connection status to bypass payout routing.

---

## Owner capture on launch

When `fundraising_post` receives a launch request (`enabled: true`) and the current state has **no** `owner_customer_id` yet:

1. **Priority 1:** The trusted `X-SS-Customer-Id` header (see [Relay header contract](#relay-header-contract-x-ss-customer-id) below).
2. **Priority 2 (fallback):** The `owner_customer_id` field on the store's `custom_shop` metaobject (written during provisioning).

The numeric id is normalised (GID stripped to bare number) before storing.

A best-effort Shopify Admin API call resolves the customer email and stores it as `owner_email`. If that call fails, launch still proceeds — `owner_email` is optional and for display only.

**Owner is sticky:** once set, `owner_customer_id` is never overwritten by a subsequent POST. The owner remains the same person until the fundraiser is stopped and a new one is started.

---

## Owner-only rule

Once a fundraiser has an `owner_customer_id`, mutating routes enforce that **only the owner or a super-admin** may proceed.

### Routes subject to owner enforcement

- `POST /api/fundraising/{handle}` — stop and edit-of-active-fundraiser paths
- `POST /api/fundraising/{handle}/stripe/connect`
- `GET /api/fundraising/{handle}/stripe/status`

### Routes NOT subject to owner enforcement (reads stay open)

- `GET /api/fundraising/{handle}` — any admin can read the fundraiser state
- `GET /api/fundraising/{handle}/public` — fully public, no auth required

### Super-admin override

Super-admins are always allowed on any of the above mutating routes.

Two super-admin signals are accepted (both require the request to have passed `_require_admin_secret` first):

1. **Env-var allowlist:** `FUNDRAISING_SUPERADMIN_CUSTOMER_IDS` — comma-separated list of numeric Shopify customer ids. Example: `FUNDRAISING_SUPERADMIN_CUSTOMER_IDS=12345,67890`.
2. **Override header:** `X-SS-Superadmin: 1` — the relay may set this header for the platform founder. This enables emergency intervention when the founder's customer id is not in the env var (e.g. during a support session).

Both signals are only honored after `_require_admin_secret` passes, so they cannot be spoofed from outside the relay.

### Failure mode when relay is not yet updated

If `X-SS-Customer-Id` is absent (relay not yet forwarding it), mutations on an owned fundraiser will return:

```json
{"ok": false, "error": "Only the fundraiser organizer can change this."}
```
with HTTP 403.

Super-admin override (`X-SS-Superadmin: 1` or a matching env-var id) still works in this case, so the platform founder is never locked out.

---

## One fundraiser at a time

`POST /api/fundraising/{handle}` rejects a **new launch** if a fundraiser is already active:

```json
{"ok": false, "error": "A fundraiser is already running for this store. Stop it before starting a new one."}
```
HTTP 409.

**Editing** an already-active fundraiser by the owner or super-admin is still allowed (same endpoint, same `enabled: true` body, but caller is the owner).

---

## Relay header contract: `X-SS-Customer-Id`

The Printful\_Automation relay sits behind the Shopify App Proxy. It verifies `logged_in_customer_id` server-side from the proxy's signed parameters, then forwards the verified numeric customer id as:

```
X-SS-Customer-Id: <numeric_customer_id>
```

This header is trusted **only when the request also passes `_require_admin_secret`** — i.e. it arrived through the authenticated relay. Never trust `X-SS-Customer-Id` on its own from an unauthenticated request.

The relay may also forward:

```
X-SS-Superadmin: 1
```

to signal that the caller is the platform founder / super-admin, enabling emergency overrides.

---

## Nuke guard

`POST /api/storefront/{handle}/nuke` now checks fundraiser state **before** spawning the deprovision job:

| Condition | Response |
|---|---|
| `state.enabled == true` | 409 `"This store has an active fundraiser. Stop the fundraiser before deleting the store."` |
| Any unpaid ledger row (`paid: false`) | 409 `"This store has unpaid fundraiser payouts in the ledger. Settle payouts before deleting the store."` |
| Neither | Proceeds normally |

### Super-admin force override

A super-admin can bypass the guard with `{"force": true}` in the request body, combined with a super-admin signal (`X-SS-Superadmin: 1` or matching env-var id). Force nukes are **logged loudly** to Railway logs for auditability:

```
⚠️ [nuke] SUPER-ADMIN FORCE NUKE for 'handle' — fundraiser is ACTIVE (force=True). Proceeding.
```

### Defense-in-depth in `_run_shopify_deprovision_job`

The background job runner re-checks `_fr_get_state(handle).get("enabled")` before shelling out. If the fundraiser became active in the window between the route guard and the thread execution, the job aborts with status `"error"` and a message explaining the situation. This prevents a direct job call from bypassing the route guard.

---

## Webhook idempotency

`POST /webhooks/fundraising/order-paid` derives a stable idempotency key before appending ledger rows:

1. `order["id"]` (numeric Shopify order id) — preferred
2. `order["admin_graphql_api_id"]` — GID fallback
3. `order["order_number"]` — string fallback
4. If **none** of the above are present, the webhook is **skipped** with a warning log and returns `{"ok": true, "skipped": "no_order_id"}`. A row is never blind-appended without a stable key.

This closes the previous bug where an empty `order_id` bypassed the deduplication check (`if order_id and any(...)`) and could double-count an order.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ADMIN_SECRET` | Yes | Shared secret for `X-Admin-Secret` header |
| `CRON_SECRET` | Yes (for payouts) | Shared secret for `X-Cron-Secret` header |
| `SHOPIFY_WEBHOOK_SECRET` | Yes | HMAC secret for Shopify webhooks |
| `STRIPE_SECRET_KEY` | Yes (for payouts) | Stripe platform secret key |
| `FUNDRAISING_HOLD_DAYS` | No (default: 7) | Days to hold a contribution before it's eligible for payout |
| `FUNDRAISING_SUPERADMIN_CUSTOMER_IDS` | No | Comma-separated numeric customer ids always allowed on any fundraiser |
| `METAOBJECT_TYPE` | No (default: `custom_shop`) | Shopify metaobject type for store records |

---

## Payout cron

The payout cron (`POST /api/fundraising/payouts/run`, requires `X-Cron-Secret`) is **OFF**. No transfer logic was changed in this PR. The cron is a separate follow-up.
