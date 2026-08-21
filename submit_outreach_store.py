"""Submit one reviewed outreach store directly to Railway.

This client is intentionally prospect-agnostic. Store data and logo bytes are
sent over HTTPS and are never written to the Git repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

import requests


TERMINAL_JOB_STATES = {"succeeded", "failed", "error"}
DEFAULT_API_BASE_URL = "https://studio-uploader-production.up.railway.app"


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def submit_store(
    *,
    base_url: str,
    admin_secret: str,
    logo_path: Path,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    with logo_path.open("rb") as logo:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/outreach/storefront-request",
            headers={"X-Admin-Secret": admin_secret},
            data=fields,
            files={
                "storefront_logo_file": (
                    logo_path.name,
                    logo,
                    "image/png" if logo_path.suffix.lower() == ".png" else "image/webp",
                )
            },
            timeout=(10, 90),
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Railway returned an invalid response")
    return payload


def wait_for_job(
    *,
    base_url: str,
    admin_secret: str,
    job_id: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    delay = 3.0
    while True:
        response = requests.get(
            f"{base_url.rstrip('/')}/api/outreach/job/{job_id}",
            headers={"X-Admin-Secret": admin_secret},
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") in TERMINAL_JOB_STATES:
            return payload
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Store build {job_id} did not finish within {timeout_seconds}s")
        time.sleep(delay)
        delay = min(20.0, delay * 1.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logo", required=True, type=Path)
    parser.add_argument("--contact-email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--type", required=True, dest="store_type")
    parser.add_argument("--color", required=True)
    parser.add_argument("--organization-url", default="")
    parser.add_argument("--contact-source-url", default="")
    parser.add_argument("--logo-source-url", default="")
    parser.add_argument("--email-authorized", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    if not args.logo.is_file():
        parser.error(f"logo file not found: {args.logo}")

    try:
        base_url = os.getenv("OUTREACH_API_BASE_URL", DEFAULT_API_BASE_URL).strip()
        admin_secret = (
            os.getenv("OUTREACH_API_SECRET", "").strip()
            or _required_environment("ADMIN_SECRET")
        )
        result = submit_store(
            base_url=base_url,
            admin_secret=admin_secret,
            logo_path=args.logo,
            fields={
                "contact_email": args.contact_email,
                "storefront_name": args.name,
                "storefront_handle": args.handle,
                "type_of_store": args.store_type,
                "primary_color": args.color,
                "organization_url": args.organization_url,
                "contact_source_url": args.contact_source_url,
                "logo_source_url": args.logo_source_url,
                "screening_confirmed": "true",
                "logo_qa_confirmed": "true",
                "email_authorized": "true" if args.email_authorized else "false",
            },
        )
        if not args.no_wait and result.get("status") == "queued" and result.get("job_id"):
            job = wait_for_job(
                base_url=base_url,
                admin_secret=admin_secret,
                job_id=str(result["job_id"]),
                timeout_seconds=max(30, args.timeout),
            )
            result = {**result, "job": job}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Direct outreach submission failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
