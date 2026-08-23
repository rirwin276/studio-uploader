"""Dressing a prospect's store in their own colours.

Every outreach store was built in the same default slate and gold, whoever it
was for. The organization's real colours were sitting in the logo the whole
time — a fire department's red, a rowing club's navy — and reading them off
the artwork costs nothing and no tokens.

It is also the cheapest signal that a person looked. A store in a stranger's
own colours reads as made for them; the same store in someone else's house
palette reads as a template with their name pasted in, which is exactly what
a prospect is deciding between when they open the link.

Best-effort throughout. A store that is built and unstyled is worth having;
failing the build over a colour is not.

The settings document below mirrors the contract in Printful_Automation's
storefront_appearance.py, which owns the schema and the editor that writes it
by hand. Keep the two in step.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import outreach_tracking


SETTINGS_FIELD_KEY = "storefront_settings"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# The neutral the default look uses. Kept as the fallback so a monochrome logo
# still produces a coherent store rather than a black one.
_FALLBACK_PRIMARY = "#1f2937"
_FALLBACK_SECONDARY = "#d4af37"


# The storefront theme picks a design from a style+pattern pair rather than
# from a name, so the names live here and the pairs go on the wire. "classic"
# is what an unset store falls through to, and it is the plainest of them —
# which is what an outreach store was getting while carrying the org's colours.
LAYOUTS: Dict[str, Tuple[str, str]] = {
    "classic": ("clean", "none"),
    "split": ("clean", "diagonal"),
    "heritage": ("clean", "stripes"),
    "gradient": ("bold", "none"),
    "spray": ("bold", "dots"),
    "pro": ("dark", "grid"),
}

# A prospect decides in about a second whether this was made for them. The
# loudest use of their own colours is the version that wins that second, so an
# unattended store opens on the boldest design rather than the safest.
DEFAULT_LAYOUT = "spray"


def layout_pair() -> Tuple[str, str]:
    """The style and pattern to build with, overridable without a deploy."""
    name = os.getenv("OUTREACH_STORE_LAYOUT", DEFAULT_LAYOUT).strip().lower()
    return LAYOUTS.get(name, LAYOUTS[DEFAULT_LAYOUT])


def _contrast(hex_color: str) -> str:
    """Black or white text, whichever survives on this background."""
    value = hex_color.lstrip("#")
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    return "#111111" if luminance > 0.58 else "#ffffff"


def _valid(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if _HEX.fullmatch(text) else fallback


def welcome_message(state: Dict[str, Any]) -> str:
    """One line under the store name, in the organization's own terms.

    Every store used to read "Approved gear for your group", which is true of
    all of them and therefore says nothing about any of them.
    """
    kind = str(state.get("type_of_store") or "").strip().lower()
    org = str(state.get("storefront_name") or "").strip()
    for suffix in (" Team Store", " Store"):
        if org.endswith(suffix):
            org = org[: -len(suffix)]
            break
    org = org.strip() or "your group"

    # Animals before emergency services: "dog rescue" contains "rescue", and
    # checked the other way round an animal shelter is greeted as a fire
    # department.
    if any(word in kind for word in ("shelter", "animal", "dog", "cat", "equine", "horse", "wildlife")):
        return f"Wear it and support {org}. Every order helps the animals."
    if any(word in kind for word in ("fire", "ems", "search and rescue", "ambulance", "paramedic")):
        return f"Department gear for {org} — members, families and supporters."
    if any(word in kind for word in ("veteran", "legion", "vfw")):
        return f"Post gear for {org} members, families and supporters."
    if any(word in kind for word in ("church", "youth group", "camp", "scout")):
        return f"Gear for {org} — for the group, the leaders and the families."
    if any(word in kind for word in ("theater", "choir", "orchestra", "band", "music")):
        return f"Show gear for {org} — cast, crew and the people in the seats."
    if any(word in kind for word in ("booster", "pto", "parent")):
        return f"Spirit wear for {org} families. Order any time, no group deadline."
    if any(word in kind for word in (
        "soccer", "baseball", "softball", "basketball", "lacrosse", "hockey",
        "rowing", "crew", "swim", "wrestling", "track", "cross country",
        "volleyball", "football", "tennis", "golf", "climbing", "archery",
        "cycling", "running", "triathlon", "sport", "team", "club",
    )):
        return f"Team gear for {org} — players, parents and the sideline."
    return f"Gear for {org} — order any time, shipped straight to you."


def settings_for(state: Dict[str, Any]) -> Dict[str, Any]:
    """The appearance document for this store."""
    colors: List[str] = [
        color for color in (state.get("brand_colors") or [])
        if _HEX.fullmatch(str(color or ""))
    ]
    primary = _valid(colors[0] if colors else None, _FALLBACK_PRIMARY)
    # The second colour only earns the accent slot if the logo actually has
    # one. Inventing a contrast colour is how a store ends up wearing a stripe
    # that appears nowhere in the organization's branding.
    secondary = _valid(colors[1] if len(colors) > 1 else None, _FALLBACK_SECONDARY)
    style, pattern = layout_pair()

    return {
        "version": 1,
        "enabled": True,
        "style": style,
        "pattern": pattern,
        "primary_color": primary,
        "secondary_color": secondary,
        "primary_text": _contrast(primary),
        "secondary_text": _contrast(secondary),
        "welcome_message": welcome_message(state)[:180],
        "announcement": "",
        "show_announcement": False,
        "catalog_enabled": True,
        "featured_enabled": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


_MUTATION = """
mutation OutreachStorefrontAppearance($id: ID!, $metaobject: MetaobjectUpdateInput!) {
  metaobjectUpdate(id: $id, metaobject: $metaobject) {
    metaobject { id }
    userErrors { field message code }
  }
}
"""


def apply(core: Any, handle: str, state: Dict[str, Any] | None = None) -> bool:
    """Dress the store. Returns whether it worked; never raises.

    A store that is built and unstyled is worth having. Failing the build over
    a colour is not, so every path out of here is a boolean.
    """
    try:
        state = state if state is not None else (outreach_tracking.read(core, handle) or {})
        shop = core._get_custom_shop(handle)
        if not shop or not shop.get("id"):
            print(f"[outreach-appearance] no store metaobject for {handle}")
            return False

        settings = settings_for(state)
        data = core._shopify_graphql(
            _MUTATION,
            {
                "id": shop["id"],
                "metaobject": {
                    "fields": [{
                        "key": SETTINGS_FIELD_KEY,
                        "value": json.dumps(settings, separators=(",", ":")),
                    }]
                },
            },
        )
        result = (data.get("metaobjectUpdate") or {}) if isinstance(data, dict) else {}
        errors = result.get("userErrors") or []
        if errors:
            print(f"[outreach-appearance] {handle}: {json.dumps(errors)[:200]}")
            return False
        return bool(result.get("metaobject"))
    except Exception as exc:
        print(f"[outreach-appearance] could not style {handle}: {exc}")
        return False
