# Stella & Sage Command Center

The Command Center is a read-only, all-store operational rollup for customers
with the exact Shopify customer tag `super-admin`. Store-scoped admins remain
limited to the handle in `storefront-admin--<handle>` or the legacy
`b2b-admin-<handle>` tag.

## Request path and trust boundary

1. The Shopify theme calls `/apps/ss/relay/...` on the Stella & Sage domain.
2. Printful_Automation verifies Shopify's App Proxy signature and the verified
   `logged_in_customer_id`.
3. The relay looks up customer tags and creates trusted `X-SS-*` headers. It
   never forwards browser-supplied identity headers.
4. The relay calls this service with the private `X-Admin-Secret`.

The raw admin secret, Shopify token, customer ID, and email address are never
sent by the storefront activity tracker.

## Endpoints

- `GET /admin/command-center/summary`
  - Requires valid `X-Admin-Secret` and `X-SS-Superadmin: 1`.
  - Returns stores, gross product sales, paid-order counts, latest purchases,
    tagged customers, non-admin members, store/platform admins, newest product,
    store age, tracked sessions, decision-review signals, and a sanitized
    recent activity log.
  - Cached for 300 seconds by default. Set `COMMAND_CENTER_CACHE_SECONDS` to
    change the TTL.
- `POST /api/store/{handle}/activity/session`
  - Requires valid `X-Admin-Secret`; public browsers reach it only through the
    signed relay.
  - Hashes the random browser session ID before persistence and stores no raw
    session ID or query string.
  - Deduplicates one session per store and retains a bounded recent event log.
  - Shopify reads and telemetry writes run off the async event loop. The theme
    waits for the page load/idle window and silently retries later on failure.

## Metric definitions

- **Gross sales:** paid product line totals at order creation, before discounts
  and refunds, attributed through the product's store-handle tag. Cancelled and
  unpaid orders are excluded.
- **Sales coverage:** lifetime only when the app has `read_all_orders` and the
  configured order cap is not reached. Otherwise the API labels totals as the
  available last 60 days, capped partial, or unavailable.
- **Tagged customers:** unique customers with a current or legacy membership or
  store-admin tag for that store. Platform super-admins are counted separately.
- **Non-admin members:** tagged store members after removing store-scoped and
  platform administrators. This is the traction number used for outreach
  review, so an owner/admin alone does not look like a shopper.
- **Effective admins:** unique store-scoped admins plus platform super-admins.
- **Sessions:** first-party storefront sessions from tracker deployment onward;
  these are not historical Shopify Online Store session analytics.
- **Last non-super-admin session:** the latest session not linked to the
  `super-admin` tag. An anonymous session is explicitly labeled anonymous and
  does not prove who the person was.
- **Last customer:** the latest tracked signed-in, non-super-admin customer
  activity. Only the Shopify display name is retained in the activity record.
- If Shopify customer tags cannot be read, the signed-in session is labeled
  `role_unknown` and is excluded from non-super-admin and last-customer metrics
  so a founder visit cannot be misclassified during an API failure.
- **Store age:** the creation timestamp of the Shopify collection that the
  existing provisioning flow creates immediately before the `custom_shop`
  record. Monitoring never adds or edits a field on the live store record.
- **No-traction review:** shown only after both the store and the tracker have
  at least seven days of evidence with zero paid orders, zero non-admin members,
  and zero non-super-admin sessions. It is a review signal only. No store is
  ever deleted, slept, or changed automatically.

## Shopify access requirements

The Studio Uploader app needs the existing metaobject/customer/product access,
plus `read_orders` for sales. `read_all_orders` is required for totals older
than 60 days. If a scope or API surface is missing, the summary must return a
data-quality warning instead of presenting a partial value as complete.

## Deployment order

1. Deploy `command_center.py` with the `fixed_app.py` installer.
2. Deploy the strict relay module and its `runtime_app.py` installer in
   Printful_Automation.
3. Confirm the signed summary and activity routes through the Shopify App Proxy.
4. Deploy the Shopify theme changes.
5. Verify a `super-admin` can open every Admin Powers page, a store admin cannot
   open another store, anonymous traffic cannot call admin summary, and a test
   session appears once in the correct store log.

Rolling back the theme tracker stops new session writes without affecting store
access. Removing the two module installers disables the reporting routes while
leaving the existing build/upload pipelines untouched.

The Command Center is not imported by `shopify_provision.py`, product builders,
checkout/order webhook handlers, Printful order submission, or fulfillment
workers. Its installer is wrapped at the end of `fixed_app.py`; an import or
route-registration failure is logged and the existing application keeps
starting.
