# Anonymous store preview — isolated test deployment

Updated September 16, 2026. The full trial is enabled only in the unpublished Shopify theme “Anonymous Store Builder Preview” (166579110138), with dedicated uploader and draft-builder Railway services. The active customer theme, main application services, order service, and homepage CTA are unchanged.

## Experience

Visitors supply a team name and logo without an account. The waiting room follows actual product readiness and includes existing product imagery, approved customer reviews, FAQs, and an optional video setting. The browser keeps its private return session. Completed builds offer a private store, design tools, and activation without automatically interrupting someone reading an FAQ.

The private store shows real Shopify draft products, variants, images and prices. The admin preview supports additional designs, existing-product editing, hide/show/remove, store name, welcome text and team colors. Artwork, placement, garment colors and personalization use the existing product editor. Copying the preview URL does not transfer the private browser session. Sharing, invitations and ordering require activation.

## Purchase and claim boundary

The isolated builder creates products as DRAFT from the initial Shopify mutation and does not publish them to the Online Store. Direct cart requests cannot buy these products. Liquid button locks are supplementary, not the security boundary.

Private bearer endpoints validate the exact anonymous ledger entry and Shopify ownership/cleanup markers. Builder tokens are scoped to the store, model and, for editing, the exact product. Ownership lookup failures deny design access. Builder authorization runs off the uploader event loop so the builder can call back to verify eligibility.

Activation uses the existing signed Shopify app-proxy join flow. A numeric verified owner and completed builds are required before the same visible draft products are published. Hidden products stay draft. A restart-safe reconciler finishes the transition; it does not create a replacement store.

## Retention

A completed preview receives 48 hours from readiness. An incomplete build has a 48-hour fallback deadline from creation. Removal runs only during 03:00–03:59 America/Los_Angeles, at the first overnight window after eligibility; daylight saving is handled. Cleanup reserves the existing atomic collection claim marker before deletion and skips claimed stores. The preview service retention flag is enabled. Scheduled overnight execution is covered by automated tests; no claimed store was deleted as a test.

Anonymous builds use their own internal status so the existing live outreach queue cannot reclassify them. Previously reclassified preview states recover when product readiness is verified.

## Verification and remaining acceptance check

72 automated tests passed: 52 uploader lifecycle/claim/preview tests, 8 isolated builder tests, and 12 browser-DOM behavior tests. A real anonymous test store completed its initial build and a further design. Browser checks covered waiting-room readiness, private products, catalog and editor. Saved appearance and hide/show were verified against the deployed API. A fresh unauthenticated direct Shopify cart request for both an initial product and an added design returned HTTP 422, “Cannot find variant.”

The connected browser uses a platform administrator account. That role deliberately does not become a prospect-store owner. Final signed-in customer claim and post-claim checkout therefore remain an acceptance check using an ordinary customer account; activation behavior is tested at the backend unit level. No purchase was placed.

## Deployment references

- Theme start: https://stellasageco.com/pages/storefront?view=start-team-store&preview_theme_id=166579110138
- Preview API: https://anonymous-demo-preview-production.up.railway.app
- Draft builder: https://anonymous-draft-builder-production.up.railway.app
- Theme PR: https://github.com/rirwin276/Shopify-code/pull/201
- Uploader PR: https://github.com/rirwin276/studio-uploader/pull/86

Keep all three feature changes together for a future main rollout. The standard theme entry setting still defaults off; the deployment manifest enables only the dedicated unpublished test theme. Do not apply unrelated staged Railway environment changes.
