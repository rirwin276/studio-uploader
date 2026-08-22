# cron_outreach_discovery.py — nightly prospect discovery runner
#
# Calls POST /api/outreach/discovery/cron on the Studio Uploader backend.
# Schedule this via Railway Cron once a night.
#
# Required env vars:
#   STUDIO_UPLOADER_URL  — defaults to the production Railway domain
#   CRON_SECRET          — shared secret validated by the backend endpoint
#
# The endpoint decides whether to run. With OPENAI_API_KEY unset or
# OUTREACH_DISCOVERY_ENABLED off it reports a skip and exits zero, so an
# unconfigured deployment does not page anyone every night.

from __future__ import annotations

import datetime
import os
import sys

import requests

STUDIO_UPLOADER_URL = os.getenv(
    "STUDIO_UPLOADER_URL",
    "https://studio-uploader-production.up.railway.app",
).strip().rstrip("/")

CRON_SECRET = os.getenv("CRON_SECRET", "").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "600"))


def run() -> int:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not CRON_SECRET:
        print(f"[{stamp}] CRON_SECRET is not set — refusing to call discovery.")
        return 1

    url = f"{STUDIO_UPLOADER_URL}/api/outreach/discovery/cron"
    print(f"[{stamp}] Running outreach discovery via {url}")
    try:
        response = requests.post(
            url,
            headers={"X-Cron-Secret": CRON_SECRET},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:
        print(f"[{stamp}] Discovery request failed: {exc}")
        return 1

    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:400]}

    if response.status_code >= 300 or not body.get("ok"):
        print(f"[{stamp}] Discovery failed (HTTP {response.status_code}): {body}")
        return 1

    if body.get("skipped"):
        print(f"[{stamp}] Skipped: {body['skipped']}")
        return 0

    run_record = body.get("run") or {}
    print(
        f"[{stamp}] Done: {run_record.get('returned', 0)} returned, "
        f"{run_record.get('accepted', 0)} accepted, "
        f"{run_record.get('queued', 0)} queued for building."
    )
    for row in run_record.get("rejected") or []:
        print(f"  skipped {row.get('handle') or '?'}: {row.get('reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
