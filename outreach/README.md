# Outreach store deployment queue

Each enabled JSON file in `pending/` is an explicitly claimable store request.
Railway processes the files sequentially on deployment. The store handle is
checked against Shopify before provisioning; an existing handle is never
rebuilt by this runner.

Logo bytes live as base64 text under `logos/` so the repository deployment can
carry the exact reviewed artwork without exposing a new upload endpoint or an
admin secret.

After a request reaches `succeeded`, remove its JSON and logo payload in a
cleanup commit. The store itself remains live in Shopify.
