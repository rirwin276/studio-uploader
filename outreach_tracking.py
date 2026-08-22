"""Small Shopify metaobject ledger for repository-controlled outreach stores.

The ledger deliberately lives beside, rather than inside, the existing
``custom_shop`` definition.  That lets the command center show outreach
dates without changing or rewriting live storefront records.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


OUTREACH_TRACKING_TYPE = "outreach_tracking"

# Every ``source`` value that means "this store was built by outreach, not by a
# customer on the website".  The multipart route writes the first value and the
# JSON intake queue writes the second; both are outreach stores and every
# consumer (prospect demo, claim tracking, retention review) must treat them the
# same.  Add new intake routes here rather than comparing to a single string.
OUTREACH_SOURCES = frozenset({
    "direct_outreach_api",
    "vendor_neutral_outreach_intake",
})


def is_outreach_source(value: Any) -> bool:
    return str(value or "").strip().lower() in OUTREACH_SOURCES


def utc_iso(value: Optional[float] = None) -> str:
    moment = datetime.now(timezone.utc) if value is None else datetime.fromtimestamp(value, timezone.utc)
    return moment.isoformat()


def parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def add_days_iso(value: Any, days: int) -> Optional[str]:
    parsed = parse_iso(value)
    return (parsed + timedelta(days=days)).isoformat() if parsed else None


def _field_map(node: Dict[str, Any]) -> Dict[str, str]:
    return {
        str(field.get("key") or ""): str(field.get("value") or "")
        for field in (node.get("fields") or [])
        if field.get("key")
    }


def ensure_definition(core: Any) -> None:
    query = """
    query OutreachTrackingDefinition($type: String!) {
      metaobjectDefinitionByType(type: $type) { id }
    }
    """
    data = core._shopify_graphql(query, {"type": OUTREACH_TRACKING_TYPE})
    if (data.get("metaobjectDefinitionByType") or {}).get("id"):
        return

    mutation = """
    mutation CreateOutreachTrackingDefinition($definition: MetaobjectDefinitionCreateInput!) {
      metaobjectDefinitionCreate(definition: $definition) {
        metaobjectDefinition { id }
        userErrors { field message code }
      }
    }
    """
    result = core._shopify_graphql(mutation, {
        "definition": {
            "type": OUTREACH_TRACKING_TYPE,
            "name": "Outreach Tracking",
            "fieldDefinitions": [
                {"key": "data", "name": "Data", "type": "json", "required": False},
            ],
        }
    })
    errors = ((result.get("metaobjectDefinitionCreate") or {}).get("userErrors")) or []
    if errors:
        raise RuntimeError("outreach tracking definition: " + json.dumps(errors, separators=(",", ":")))


def read(core: Any, handle: str) -> Dict[str, Any]:
    query = """
    query OutreachTrackingByHandle($handle: MetaobjectHandleInput!) {
      metaobjectByHandle(handle: $handle) { fields { key value } }
    }
    """
    data = core._shopify_graphql(query, {
        "handle": {"type": OUTREACH_TRACKING_TYPE, "handle": handle},
    })
    raw = _field_map(data.get("metaobjectByHandle") or {}).get("data") or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def upsert(core: Any, handle: str, state: Dict[str, Any]) -> None:
    ensure_definition(core)
    mutation = """
    mutation UpsertOutreachTracking($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
      metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
        metaobject { id handle }
        userErrors { field message code }
      }
    }
    """
    result = core._shopify_graphql(mutation, {
        "handle": {"type": OUTREACH_TRACKING_TYPE, "handle": handle},
        "metaobject": {
            "fields": [{"key": "data", "value": json.dumps(state, separators=(",", ":"))}],
        },
    })
    errors = ((result.get("metaobjectUpsert") or {}).get("userErrors")) or []
    if errors:
        raise RuntimeError("outreach tracking upsert: " + json.dumps(errors, separators=(",", ":")))


def update(core: Any, handle: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    state = read(core, handle)
    state.update(patch)
    upsert(core, handle, state)
    return state


def list_all(core: Any) -> Dict[str, Dict[str, Any]]:
    query = """
    query OutreachTrackingNodes($type: String!, $after: String) {
      metaobjects(type: $type, first: 100, after: $after) {
        nodes { handle fields { key value } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    out: Dict[str, Dict[str, Any]] = {}
    cursor: Optional[str] = None
    while True:
        data = core._shopify_graphql(query, {
            "type": OUTREACH_TRACKING_TYPE,
            "after": cursor,
        })
        connection = data.get("metaobjects") or {}
        for node in connection.get("nodes") or []:
            handle = str(node.get("handle") or "").strip().lower()
            raw = _field_map(node).get("data") or ""
            if not handle or not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                out[handle] = parsed
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            return out
        cursor = str(page["endCursor"])
