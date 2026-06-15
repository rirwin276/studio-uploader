# studio-uploader — Cleanup & Security Notes

A running log of intentional changes, why they were made, and anything left unfinished.
Newest entries on top.

---

## 2026-06-15 — Closed the unauthenticated `/nuke` bypass (security)

**File:** `app.py` — `storefront_nuke()` (the `POST /api/storefront/{handle}/nuke` route)

**Problem (before):**
The nuke endpoint (full destructive store teardown: deletes products, collection,
metaobject, customer tags, logo files) only checked authorization *if* a `customer_id`
was present in the request body. An empty-body POST — i.e. no secret and no customer_id —
**proceeded with zero checks** ("trust is enforced at the UI layer"). Anyone who could
reach the URL and knew/guessed a store handle could permanently destroy that store.

**Change:**
Replaced the conditional check with an explicit authorization gate. A nuke is now allowed
only if one of these is true, otherwise it returns **401**:
1. A valid `X-Admin-Secret` header (super-admin / Admin Powers page). Reuses the existing
   `_require_admin_secret(request)` helper.
2. `customer_id` in the body that holds `storefront-admin--{handle}` **or** the
   `super-admin` tag (the store's own owner, e.g. the Danger tab).

The existing store-owner path (customer_id + storefront-admin tag) is preserved unchanged,
so the Danger-tab nuke keeps working. The docstring was updated to match.

**⚠️ DEPLOY ORDER — IMPORTANT (do not skip):**
The Shopify **Admin Powers** nuke button currently sends NO secret and NO body
(`Shopify-code/sections/admin-powers-page.liquid`, the `/nuke` fetch ~line 3205).
After this backend change, that button will get a 401 until the theme is updated to send
`X-Admin-Secret`. Therefore:
  1. FIRST: update + publish the theme so the nuke fetch sends
     `'X-Admin-Secret': AP_ADMIN_SECRET`.
  2. Verify the Admin Powers nuke still works against the OLD backend (it will — the extra
     header is ignored).
  3. THEN: deploy this studio-uploader change.
Deploying this backend change before the theme update will break the Admin Powers nuke.

**Note on the secret:** `AP_ADMIN_SECRET` / `ADMIN_SECRET` is still the shared
`stellasage-god-mode-2026-xK9mP` value and is visible in the public theme JS. This change
raises the bar from "anyone can nuke" to "anyone with the (still-public) secret can nuke,"
and makes nuke consistent with every other admin endpoint. The real fix remains moving the
admin gate to server-side permission validation (no browser-visible master secret). When
the secret is rotated, update BOTH the backend `ADMIN_SECRET` env var AND the theme value
together, or nuke will break.

**Status:** committed on branch `claude/dazzling-turing-76cjur`. NOT merged to main.
Recover the previous version from git history if needed.
