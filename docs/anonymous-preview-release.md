# Anonymous store preview: implementation and release status

This draft ports the older preview code onto main as of September 15, 2026. It does not enable the public entry point or change the active theme.

## Visitor experience

Team name and logo are required; group type and color are optional. No email or account is required. The waiting room uses actual backend stages, includes Cashmere product imagery, explains the next steps, and answers questions about setup fees, sizes/payment collection, personalization, fabrics/care, delivery and keeping the preview.

The existing approved featured-review section is reused inside the waiting room. It renders nothing when there are no approved reviews or its service fails. A Shopify-hosted how-to video can be added through the section setting; no empty video placeholder is shown.

Progress is saved in the current browser. Readiness does not redirect someone away from an FAQ or video. Store and design-tool links appear when products are ready. Requests time out and retry; stale return links offer a new start. The claim handoff retains the same store and existing products.

## Companion backend changes

Use the matching studio-uploader draft. Provision acknowledgement is no longer treated as product readiness. The normal is_fully_ready metaobject flag is checked before readiness and the product-marker pass. Anonymous previews may create sequential additional products; the older outreach demo retains its one-product allowance. The response includes last_product_status so the UI can show success while the next builder is available.

Deletion eligibility starts 48 hours after readiness; incomplete builds have an initial 48-hour deadline to avoid indefinite orphaning. Removal runs only during 03:00–03:59 America/Los_Angeles and honors daylight saving. Missed overnight windows wait for a later overnight pass. The UI shows the next scheduled removal date.

## Release blockers — do not enable yet

1. The inherited product tag plus Liquid purchase-button lock is UI-only. Direct Shopify cart/checkout must be enforced before enabling. Automatic products may also be published before the end-of-build tagging pass. A safe production release requires a verified Shopify checkout validation or a draft-product preview path with activation publishing; do not represent the current tag as server-enforced checkout protection.
2. The inherited admin demo supports appearance and new-product builders. Existing-product edit/hide/delete and store identity remain locked; those need scoped anonymous endpoints before claiming this is the complete admin trial requested.
3. Cleanup now reserves the same atomic Shopify claim marker before removal. Claim requests reject the deletion marker with HTTP 410. A failed cleanup keeps the marker so a partially removed store cannot be claimed; the next overnight pass can retry. Verify this against Shopify in staging before enabling cleanup.
4. Validate the entire anonymous upload → automatic product build → additional design → signed Shopify login → same-store claim in a non-production environment. Test direct cart URLs, sharing/invites and post-claim unlock. Do not restore the stale anonymous feature branch over main.
5. A rendered browser QA pass is still required. The cloud browser could not open the local review server; automated DOM tests are provided, but these do not prove visual rendering.

The design-review HTML now contains only the waiting-room state, with no signup form or account-choice cards. It renders the waiting state even when scripts are disabled.

## Validation

Nine DOM behavior tests cover building, ready, failure recovery, expired-session reset, claimed handoff, network failure and untrusted returned URLs, team identity, and the waiting-only preview with scripts disabled. Backend tests cover readiness, multi-build reservation behavior, owner/claim-marker protection, daylight-saving dates and the no-daytime-deletion guard.

## Scope safety

The public demo setting defaults off. No homepage CTA has been switched. Production checkout, orders, live customer stores and Railway settings have not been changed.
