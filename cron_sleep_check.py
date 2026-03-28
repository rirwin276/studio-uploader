# cron_sleep_check.py — Daily cron runner for the sleep-mode check
#
# Calls POST /admin/sleep-check/cron on the Studio Uploader backend.
# Schedule this via Railway Cron, an external scheduler, or run manually.
#
# Required env vars:
#   STUDIO_UPLOADER_URL  — defaults to https://studio-uploader-production.up.railway.app
#   CRON_SECRET          — shared secret validated by the backend endpoint

from __future__ import annotations

import os
import sys
import datetime

import requests

STUDIO_UPLOADER_URL = os.getenv(
    "STUDIO_UPLOADER_URL",
    "https://studio-uploader-production.up.railway.app",
).strip().rstrip("/")

CRON_SECRET = os.getenv("CRON_SECRET", "").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))


def run() -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] Running sleep check via {STUDIO_UPLOADER_URL}/admin/sleep-check/cron")

    if not CRON_SECRET:
        print("⚠️  CRON_SECRET is not set — the endpoint may reject this request")

    url = f"{STUDIO_UPLOADER_URL}/admin/sleep-check/cron"
    headers = {"X-Cron-Secret": CRON_SECRET}

    try:
        r = requests.post(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        print(f"✅ Response {r.status_code}: {r.text[:500]}")
    except requests.HTTPError as e:
        print(f"❌ HTTP error: {e} — body: {e.response.text[:500] if e.response is not None else 'n/a'}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Request failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run()
