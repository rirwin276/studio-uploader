"""Queue one screened store through the vendor-neutral Railway intake API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict

import requests


DEFAULT_API_BASE_URL = "https://studio-uploader-production.up.railway.app"
TERMINAL_STATES = {
    "existing",
    "existing_store",
    "provisioned",
    "intake_failed",
    "failed",
}


def _authentication() -> tuple[str, str]:
    outreach_secret = os.getenv("OUTREACH_API_SECRET", "").strip()
    if outreach_secret:
        return "X-Outreach-Secret", outreach_secret
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if admin_secret:
        return "X-Admin-Secret", admin_secret
    raise RuntimeError("OUTREACH_API_SECRET is not configured")


def queue_store(
    *,
    base_url: str,
    header_name: str,
    secret: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    response = requests.post(
        f"{base_url.rstrip('/')}/api/outreach/intake",
        headers={header_name: secret},
        json=payload,
        timeout=(10, 30),
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("Railway returned an invalid intake response")
    return result


def wait_for_store(
    *,
    base_url: str,
    header_name: str,
    secret: str,
    handle: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    delay = 5.0
    while True:
        response = requests.get(
            f"{base_url.rstrip('/')}/api/outreach/intake/{handle}",
            headers={header_name: secret},
            timeout=(10, 30),
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") in TERMINAL_STATES:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Store intake {handle} did not finish within {timeout_seconds}s"
            )
        time.sleep(delay)
        delay = min(30.0, delay * 1.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-request-id", required=True)
    parser.add_argument("--source-agent", default="openai")
    parser.add_argument("--contact-email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--type", required=True, dest="store_type")
    parser.add_argument("--color", required=True)
    parser.add_argument("--organization-url", required=True)
    parser.add_argument("--contact-source-url", required=True)
    parser.add_argument("--logo-source-url", required=True)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    try:
        base_url = os.getenv("OUTREACH_API_BASE_URL", DEFAULT_API_BASE_URL).strip()
        header_name, secret = _authentication()
        queued = queue_store(
            base_url=base_url,
            header_name=header_name,
            secret=secret,
            payload={
                "provider_request_id": args.provider_request_id,
                "source_agent": args.source_agent,
                "contact_email": args.contact_email,
                "storefront_name": args.name,
                "storefront_handle": args.handle,
                "type_of_store": args.store_type,
                "primary_color": args.color,
                "organization_url": args.organization_url,
                "contact_source_url": args.contact_source_url,
                "logo_source_url": args.logo_source_url,
                "screening_confirmed": True,
                "logo_source_reviewed": True,
                "email_authorized": False,
            },
        )
        result = queued
        if not args.no_wait and queued.get("status") == "intake_queued":
            status = wait_for_store(
                base_url=base_url,
                header_name=header_name,
                secret=secret,
                handle=args.handle,
                timeout_seconds=max(30, args.timeout),
            )
            result = {**queued, "job": status}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Outreach intake failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
