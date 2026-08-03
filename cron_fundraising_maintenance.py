# cron_fundraising_maintenance.py — Daily fundraiser housekeeping
#
# Calls POST /api/fundraising/maintenance on the Studio Uploader backend.
# Schedule this via Railway Cron to run once a day.
#
# It does two things, both of which need to happen more often than payouts:
#
#   1. Closes any campaign past its end date and restores that store's prices.
#      A fundraiser that ended on Saturday should not still be raising prices
#      on Thursday, and the shopper-facing bar already promised it would stop.
#
#   2. Folds settled, aged-out ledger rows into a running total, so a campaign
#      that sells well cannot outgrow the metaobject field it lives in.
#
# Both are idempotent — running it twice in a day changes nothing the second
# time, and a missed day is caught up on the next run.
#
# Required env vars:
#   STUDIO_UPLOADER_URL  — defaults to https://studio-uploader-production.up.railway.app
#   CRON_SECRET          — shared secret validated by the backend endpoint

from __future__ import annotations

import datetime
import json
import os
import sys

import requests

STUDIO_UPLOADER_URL = os.getenv(
    "STUDIO_UPLOADER_URL",
    "https://studio-uploader-production.up.railway.app",
).strip().rstrip("/")

CRON_SECRET = os.getenv("CRON_SECRET", "").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "300"))


def run() -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{STUDIO_UPLOADER_URL}/api/fundraising/maintenance"
    print(f"[{ts}] Running fundraiser maintenance via {url}")

    if not CRON_SECRET:
        print("⚠️  CRON_SECRET is not set — the endpoint will reject this request")

    try:
        r = requests.post(
            url,
            headers={"X-Cron-Secret": CRON_SECRET, "Content-Type": "application/json"},
            json={},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        detail = e.response.text[:1000] if e.response is not None else "n/a"
        print(f"❌ HTTP error: {e} — body: {detail}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Request failed: {e}")
        sys.exit(1)

    try:
        data = r.json()
    except Exception:
        print(f"✅ Completed (non-JSON response): {r.text[:500]}")
        return

    results = data.get("results") or []
    closed = [x for x in results if x.get("closed")]
    archived = sum(int(x.get("ledger_rows_archived") or 0) for x in results)
    errors = [x for x in results if x.get("close_error") or x.get("compact_error")]

    print(f"✅ Checked {len(results)} fundraiser(s): "
          f"{len(closed)} closed, {archived} ledger row(s) archived")
    for c in closed:
        print(f"   ended: {c['handle']} (raised ${float(c.get('total_raised') or 0):.2f})")
    for e in errors:
        print(f"   ⚠️ {e.get('handle')}: {e.get('close_error') or e.get('compact_error')}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    run()
