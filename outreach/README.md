# Direct outreach automation

Prospect records and artwork do not belong in this repository. Automated
outreach stores are submitted directly to Railway through the authenticated
multipart route:

`POST /api/outreach/storefront-request`

The request prefers the outreach-only `X-Outreach-Secret` authentication backed
by a production `OUTREACH_API_SECRET` of at least 32 characters and uploads the
reviewed logo as `storefront_logo_file`. Existing `X-Admin-Secret`
authentication remains available for backward compatibility. The response
includes the build job id, preview URL, and first-claimant administration URL.
Status is available at `GET /api/outreach/job/{job_id}` using the same header.

`submit_outreach_store.py` is the reusable low-cost worker client. It defaults
to the production studio-uploader Railway URL. When `OUTREACH_API_SECRET` is
configured it sends `X-Outreach-Secret`; otherwise it falls back to the legacy
`ADMIN_SECRET` / `X-Admin-Secret` pair. Secrets are never stored in source
control. The client handles status polling internally so an AI model receives
only the final job result instead of spending reasoning calls watching
Printful.

The direct route defaults email authorization off, raises the tri-blend and
kids hoodie artwork by approximately one inch, prevents duplicate builds by
store handle, and records build state in the Shopify outreach-tracking
metaobject used by the Command Center.

After the initial email is actually sent, call:

`POST /api/outreach/store/{handle}/mark-sent`

That starts the three-day follow-up and seven-day deletion-review clocks. Store
deletion continues to use the authenticated `/api/storefront/{handle}/nuke`
route so fundraiser safeguards remain in force.
