# cron_payout_run.py — Weekly cron runner for fundraiser payouts
#
# Calls POST /api/fundraising/payouts/run on the Studio Uploader backend.
# Schedule this via Railway Cron (recommended: Friday morning) or run manually.
#
# The payout endpoint is idempotent: re-running the same eligible set on the
# same Friday never creates a second Stripe Transfer. Running with nothing due
# is a safe no-op.
#
# Required env vars:
#   STUDIO_UPLOADER_URL  — defaults to https://studio-uploader-production.up.railway.app
#   CRON_SECRET          — shared secret validated by the backend endpoint
#
# Optional:
#   FUNDRAISING_PAYOUT_HANDLE — limit the run to a single store handle (testing).
#                               A CLI arg (cron_payout_run.py <handle>) overrides it.

from __future__ import annotations

import os
import sys
import json
import datetime

import requests

STUDIO_UPLOADER_URL = os.getenv(
    "STUDIO_UPLOADER_URL",
    "https://studio-uploader-production.up.railway.app",
).strip().rstrip("/")

CRON_SECRET = os.getenv("CRON_SECRET", "").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "120"))
PAYOUT_HANDLE = os.getenv("FUNDRAISING_PAYOUT_HANDLE", "").strip()


def run() -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{STUDIO_UPLOADER_URL}/api/fundraising/payouts/run"

    # A CLI arg overrides the env var for a one-off single-store test.
    handle = (sys.argv[1].strip() if len(sys.argv) > 1 else PAYOUT_HANDLE)
    scope = f"handle={handle}" if handle else "all stores"
    print(f"[{ts}] Running fundraiser payout via {url} ({scope})")

    if not CRON_SECRET:
        print("⚠️  CRON_SECRET is not set — the endpoint will reject this request")

    headers = {"X-Cron-Secret": CRON_SECRET, "Content-Type": "application/json"}
    body = {"handle": handle} if handle else {}

    try:
        r = requests.post(url, headers=headers, json=body, timeout=HTTP_TIMEOUT)
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
        print(f"✅ Response {r.status_code}: {r.text[:1000]}")
        return

    print(f"✅ Response {r.status_code}:\n{json.dumps(data, indent=2)[:4000]}")

    # Surface per-handle failures (the top-level ok can be True while an
    # individual store errored). Exit non-zero so the scheduler flags it, but
    # do NOT treat a normal "nothing due" run as a failure.
    results = data.get("results") or []
    errored = [x for x in results if x.get("error")]
    if data.get("ok") is False or errored:
        bad = [x.get("handle") for x in errored]
        print(f"⚠️  Completed with problems: ok={data.get('ok')}, errored_handles={bad}")
        sys.exit(2)


if __name__ == "__main__":
    run()
