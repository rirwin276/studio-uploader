# Fundraiser System — Test Readiness Runbook

> **Branch:** `claude/dazzling-turing-76cjur`
> **Audience:** Ryan (operator) — how to wire the secrets, verify the environment,
> run a manual payout, and execute the first end-to-end $1 fundraiser test.

This runbook covers the operational steps. The code-level design lives in
[`fundraising-backend.md`](./fundraising-backend.md).

---

## 1. The `CRON_SECRET` — what it is and where it goes

`CRON_SECRET` is **not** from Stripe. **You create it** as a long random string
(e.g. `openssl rand -hex 32`). It authenticates the scheduled payout job to the
backend. It is sent only as an HTTP header (`X-Cron-Secret`) from server-to-server.

**Where to set it (backend env vars only — never in theme/browser code):**

| Service | Needs `CRON_SECRET`? | Why |
|---|---|---|
| **studio-uploader** (Railway) | ✅ Yes | Validates `X-Cron-Secret` on payout + sleep-check endpoints |
| **the cron job / scheduler** (Railway Cron) | ✅ Yes | Sends `X-Cron-Secret` when calling the endpoint |
| Printful_Automation (relay) | ❌ No | The relay never touches the payout-run endpoint |
| Shopify theme / browser | ❌ **Never** | Would leak the secret publicly |

The same string must be identical on studio-uploader and on whatever runs the
cron (same Railway project env, or duplicated into the cron service env).

---

## 2. Required environment variables

### studio-uploader (the money service)

| Var | Purpose |
|---|---|
| `ADMIN_SECRET` | Auth for relay → backend admin calls; also accepted by the health probe |
| `CRON_SECRET` | Auth for the payout-run + sleep-check cron endpoints |
| `SHOPIFY_WEBHOOK_SECRET` | Verifies Shopify `orders/paid` webhook HMAC |
| `STRIPE_SECRET_KEY` | `sk_test_…` for testing, `sk_live_…` for production |
| `SHOP` | `your-store.myshopify.com` |
| `CLIENT_SECRET` | Shopify Admin API access token |
| `FUNDRAISING_SUPERADMIN_CUSTOMER_IDS` | Comma-separated numeric customer ids with super-admin |
| `FUNDRAISING_HOLD_DAYS` | Optional; defaults to `7` (days a contribution is held before payout) |

### Printful_Automation (the relay)

| Var | Purpose |
|---|---|
| `ADMIN_SECRET` | Injected as `X-Admin-Secret` into studio-uploader (must match) |
| `SHOPIFY_APP_PROXY_SECRET` | Verifies the Shopify App Proxy signature |
| `STUDIO_UPLOADER_URL` | Base URL of studio-uploader (has a prod default) |
| `FUNDRAISING_SUPERADMIN_CUSTOMER_IDS` | Must match studio-uploader's value |
| `SHOP`, `CLIENT_SECRET` | Shopify Admin API (customer-tag lookups for super-admin) |

---

## 3. Verify the environment (health probes)

Both services expose a probe that reports **present/missing** per variable and
**never reveals a value**. Run these before testing.

**studio-uploader** (auth with the cron secret OR the admin secret):

```bash
curl -s https://studio-uploader-production.up.railway.app/healthz/fundraising \
  -H "X-Cron-Secret: $CRON_SECRET" | jq
```

Expected when ready:

```json
{
  "ok": true,
  "service": "studio-uploader",
  "ready_for_test": true,
  "env": { "ADMIN_SECRET": "present", "CRON_SECRET": "present", "...": "present" },
  "missing": [],
  "stripe_mode": "test",
  "payout_hold_days": 7,
  "next_payday": "2026-06-26"
}
```

If `ready_for_test` is `false`, the `missing` array names exactly which vars to set.

**Printful_Automation relay** (auth with `ADMIN_SECRET`):

```bash
curl -s https://printfulautomation-production.up.railway.app/api/fundraising/relay-health \
  -H "X-Admin-Secret: $ADMIN_SECRET" | jq
```

This also pings studio-uploader and reports `studio_uploader.reachable`.

---

## 4. Running the payout job

### Automatic (recommended: Railway Cron, Friday morning)

The runner script is `cron_payout_run.py`. Schedule it in Railway Cron:

- **Command:** `python cron_payout_run.py`
- **Schedule (cron):** `0 14 * * 5` — Fridays 14:00 UTC (adjust to your morning)
- **Env:** `CRON_SECRET` and `STUDIO_UPLOADER_URL` available to the cron service.

The job is safe to run anytime:
- No fundraisers / no connected Stripe accounts / nothing past the hold → clean no-op.
- It will **not** double-pay: re-running the same eligible set on the same Friday
  reuses a Stripe idempotency key and a durable batch marker.
- Per-handle results are printed; the script exits non-zero if any handle errored.

### Manual (for the first test) — single store

Test one fake store first by passing its handle:

```bash
curl -s -X POST \
  https://studio-uploader-production.up.railway.app/api/fundraising/payouts/run \
  -H "X-Cron-Secret: $CRON_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"handle":"your-test-store-handle"}' | jq
```

Or with the runner script:

```bash
CRON_SECRET=... python cron_payout_run.py your-test-store-handle
```

Example result:

```json
{
  "ok": true,
  "results": [
    { "handle": "your-test-store-handle", "transferred": 1.0, "transfer_id": "tr_..." }
  ]
}
```

Possible per-handle outcomes: `transferred` (paid), `skipped: "no connected stripe
account"`, `skipped: "insufficient_stripe_balance"` (with `due_cents`/`available_cents`),
`skipped: "already_paid_this_batch"`, `transferred: 0` (nothing past the hold), or `error`.

> ⚠️ `CRON_SECRET` is required and must stay on the backend. Do **not** add a
> browser button that posts the secret — run payouts from a server/terminal only.

---

## 5. Payout summary (read-only control panel)

Super-admins can view what's owed without moving money:

```bash
curl -s https://printfulautomation-production.up.railway.app/relay/admin/fundraising/payouts/summary \
  ...App-Proxy-signed request from the storefront Fundraiser Hub...
```

In practice you open it from the storefront **Fundraiser Hub** (super-admin only).
It shows next payday, Stripe available balance, total eligible / on-hold / unpaid,
load-needed, and which stores are missing a connected Stripe account. It never
moves money and degrades gracefully if the Stripe balance call fails.

---

## 6. Shopify `orders/paid` webhook

The webhook endpoint is:

```
POST https://studio-uploader-production.up.railway.app/webhooks/fundraising/order-paid
```

Install it in Shopify (Settings → Notifications → Webhooks, or via the Admin API)
as topic **`orders/paid`**, format JSON. It:
- verifies the HMAC with `SHOPIFY_WEBHOOK_SECRET` (rejects invalid),
- finds fundraiser-tagged line items, adds `amount × quantity` to `total_raised`,
  and appends one unpaid ledger row,
- is idempotent per order id (duplicate deliveries do not double-count),
- skips cleanly when no fundraiser products are present or the fundraiser is off.

If `SHOPIFY_WEBHOOK_SECRET` is unset, the endpoint **fails closed** (rejects all).

---

## 7. First end-to-end test ($1 fundraiser)

1. **Pre-flight:** both health probes show `ready_for_test: true`; `stripe_mode: test`.
2. **Store:** use a fake/private store where you are owner/super-admin.
3. **Stripe:** connect Stripe Express (test mode) from the fundraiser UI; confirm
   status shows connected.
4. **Launch:** amount `$1`, small goal, public bar on. Pricing status goes
   `pending` → wait 3–4 min → `succeeded`. Confirm customer price rose by **$2**
   ($1 cause + $1 platform fee).
5. **Order:** place a test purchase; confirm Shopify marks it paid; confirm a
   single ledger row appears and `total_raised` increased by `$1 × qty`. Re-deliver
   the webhook (Shopify "Send test notification" or replay) → confirm **no** second row.
6. **Summary:** open the Fundraiser Hub payout summary; confirm the store, amounts,
   Stripe balance, and load-needed are correct.
7. **Payout:** load the Stripe **test** balance if needed, then run the manual
   single-store payout (section 4). Confirm exactly **one** Transfer is created and
   the ledger row flips to paid. **Run it again → confirm no second Transfer.**
   (Note: a contribution is only eligible after the `FUNDRAISING_HOLD_DAYS` hold —
   default 7 days. For a same-day payout test, temporarily set `FUNDRAISING_HOLD_DAYS=0`
   on studio-uploader, or back-date is not needed because the run measures the hold
   against *now*.)
8. **Stop:** stop the fundraiser; confirm the public bar hides and prices restore
   **exactly** from the saved snapshot. Confirm the store cannot be nuked while
   unpaid ledger rows remain.

---

## 8. Notes / known follow-ups

- **Hold period gotcha for same-day testing:** the payout run only pays rows whose
  `created_at` is older than `FUNDRAISING_HOLD_DAYS` (default 7) relative to *now*.
  For an immediate test payout, set `FUNDRAISING_HOLD_DAYS=0` on studio-uploader,
  run the test, then restore it to `7`.
- **Ledger source of truth:** today the ledger lives in the Shopify metaobject JSON
  blob. That is fine for the MVP test. A database-backed ledger + reconciliation
  audit job is the recommended next safety improvement (see handoff Task 10) and is
  intentionally **not** built before the first test.
