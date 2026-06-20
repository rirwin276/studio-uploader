# Fundraising Backend — Stella & Sage

> **Branch:** `claude/dazzling-turing-76cjur`  
> **Payout cron:** A runner script (`cron_payout_run.py`) and a manual single-store
> path now exist. The payout endpoint stays idempotent and must be wired to Railway
> Cron with `CRON_SECRET`. Operational steps: [`fundraising-test-runbook.md`](./fundraising-test-runbook.md).

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

## Payout summary dashboard

### `GET /api/fundraising/payouts/summary`

Read-only endpoint for the platform founder. Returns the upcoming Friday payout date, per-store eligibility breakdown, current Stripe balance, and how much needs to be loaded by Thursday.

**Auth:** `X-Admin-Secret` must pass AND the caller must be a super-admin (`X-SS-Superadmin: 1` or `X-SS-Customer-Id` in `FUNDRAISING_SUPERADMIN_CUSTOMER_IDS`). Returns 403 otherwise.

**This endpoint never moves money.** The only Stripe call it makes is `stripe.Balance.retrieve()`.

#### Friday/Thursday business model

Payouts settle weekly on **Friday**. The platform founder funds the Stripe platform balance on **Thursday** so transfers clear by Friday. The dashboard answers, any day of the week:

- What is the **next Friday** (upcoming payout date)?
- For each store: how much is **eligible to be paid this coming Friday** (unpaid rows whose 7-day hold will have cleared by that Friday), how much is **still on hold**, and the store's total unpaid amount.
- Platform-wide **total to pay this Friday**, **current Stripe available balance**, and **shortfall to load** = max(0, total_eligible_by_friday − stripe_available).

A row is counted as "eligible by Friday" when `created_at <= (next_friday − FUNDRAISING_HOLD_DAYS)`. If today is Friday, today is used as the payout date.

If `stripe.Balance.retrieve()` fails, `stripe_available` is `null` and `stripe_error` is set, but the per-store breakdown is still returned (the dashboard does not 500 because Stripe is unavailable).

#### Response shape

```json
{
  "ok": true,
  "next_payday": "2026-06-26",
  "stripe_available": 0.00,
  "stripe_error": null,
  "totals": {
    "total_eligible_by_friday": 0.00,
    "total_eligible_including_unconnected": 0.00,
    "total_on_hold": 0.00,
    "load_needed": 0.00
  },
  "stores": [
    {
      "handle": "...",
      "cause_name": "...",
      "stripe_connected": true,
      "stripe_account_id": "acct_...",
      "eligible_by_friday": 0.00,
      "on_hold": 0.00,
      "total_unpaid": 0.00,
      "eligible_row_count": 0
    }
  ]
}
```

`total_eligible_by_friday` sums only `stripe_connected` stores (only connected accounts can receive transfers). `total_eligible_including_unconnected` shows money owed to stores not yet onboarded. `load_needed` is `null` when `stripe_available` is null.

---

## Payout cron safety (payout_batches state field)

The payout cron (`POST /api/fundraising/payouts/run`, requires `X-Cron-Secret`) is **OFF**. The cron will not run automatically and no transfers are triggered by this PR.

The function has been hardened with two money-safety improvements:

### 1. Stripe idempotency key

`stripe.Transfer.create` now receives an `idempotency_key` derived from `(handle, next_friday_iso, sorted_order_ids_hash)`:

```
fr-payout:{handle}:{friday_iso}:{md5_of_sorted_order_ids[:12]}
```

Re-running the cron on the same Friday for the same handle with the same eligible rows will produce the same key. Stripe's idempotency guarantee prevents a second transfer from being created.

### 2. Durable payout batch marker (`payout_batches`)

Before creating a transfer, the cron writes a `"pending"` record to `state["payout_batches"]` and persists it. If the server crashes after the transfer is created but before the ledger rows are marked paid, a retry will find the pending marker, re-issue the transfer call with the same idempotency key (Stripe returns the original transfer), and complete the mark-as-paid step.

`payout_batches` is a server-managed array of records:

| Field | Type | Description |
|---|---|---|
| `batch_id` | str | Stable idempotency key (see format above) |
| `handle` | str | Store handle |
| `friday` | str | ISO date of the payout Friday |
| `order_ids` | list[str] | Sorted eligible order ids |
| `amount` | float | USD amount transferred |
| `status` | str | `"pending"` or `"paid"` |
| `transfer_id` | str | Stripe transfer id (set when status → `"paid"`) |
| `created_at` | str | ISO timestamp of when the batch was written |

A handle with a `"paid"` batch record for the current Friday is skipped entirely — no Stripe call, no duplicate mark.

Skipped handles (insufficient balance) never receive a pending marker, so there are no dangling pending records.

### State field reference update

| Field | Type | Writable by | Notes |
|---|---|---|---|
| `payout_batches` | list | **server only** | Written by `fundraising_payouts_run`; tracks pending/paid transfer batches for double-pay prevention |
