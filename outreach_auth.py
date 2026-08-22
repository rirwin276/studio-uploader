"""Authentication shared by Stella & Sage outreach-only endpoints."""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import Request


MIN_OUTREACH_SECRET_LENGTH = 32


def require_outreach_secret(core: Any, request: Request):
    """Authorize the scoped outreach key, with legacy admin fallback."""
    configured = os.getenv("OUTREACH_API_SECRET", "").strip()
    supplied = request.headers.get("X-Outreach-Secret", "").strip()
    if (
        len(configured) >= MIN_OUTREACH_SECRET_LENGTH
        and supplied
        and secrets.compare_digest(supplied, configured)
    ):
        return None
    return core._require_admin_secret(request)
