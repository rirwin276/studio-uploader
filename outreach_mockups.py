"""Two product photos to put in the outreach email.

A link asks somebody to imagine it. A picture of their own logo on a shirt
does the imagining for them, and that is the difference between an email that
gets opened and one that gets read.

The photos are the store's real listings, not a generic catalogue shot, so
what arrives in the inbox is exactly what is waiting behind the link.

Everything here is best-effort by design. The email is worth sending without
pictures and is not worth failing to send because a CDN was slow, so every
path out of this module ends in "fewer photos", never in an exception the
caller has to think about.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests


# Shopify serves these at print resolution. An email does not need that, and a
# 4 MB message is a deliverability problem all on its own.
_CDN_WIDTH = 720
_MAX_BYTES_EACH = 900_000
_MAX_BYTES_TOTAL = 1_800_000
_TIMEOUT = (5, 15)

_QUERY = """
query OutreachStoreProducts($query: String!) {
  products(first: 30, query: $query, sortKey: CREATED_AT) {
    edges {
      node {
        title
        status
        featuredImage { url altText }
      }
    }
  }
}
"""

# Ordered: the first pattern that matches a title wins the slot. "Tri-blend"
# before the generic tee because it is the one the email names, and a hoodie
# before a crewneck for the same reason.
_SLOTS = (
    ("tee", ("tri-blend", "triblend", "tri blend")),
    ("tee", ("t-shirt", "tee", "shirt")),
    ("hoodie", ("hoodie", "hooded")),
    ("hoodie", ("crewneck", "sweatshirt", "pullover")),
)


def _titled(node: Dict[str, Any]) -> str:
    return str(node.get("title") or "").strip().lower()


def _products(core: Any, handle: str) -> List[Dict[str, Any]]:
    data = core._shopify_graphql(_QUERY, {"query": f"tag:{handle}"})
    rows = []
    for edge in ((data.get("products") or {}).get("edges") or []):
        node = (edge or {}).get("node") or {}
        image = (node.get("featuredImage") or {}).get("url")
        if not image:
            continue
        rows.append({
            "title": str(node.get("title") or "").strip(),
            "url": str(image),
            "alt": str((node.get("featuredImage") or {}).get("altText") or "").strip(),
        })
    return rows


def _choose(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One tee and one hoodie, or the best two available.

    Filling a slot with anything rather than leaving it empty is deliberate: a
    store built from a catalogue that changed still has garments worth showing,
    and the prospect cannot tell which slot the picker meant to fill.
    """
    chosen: List[Dict[str, Any]] = []
    taken: set[str] = set()
    filled: set[str] = set()

    for slot, patterns in _SLOTS:
        if slot in filled:
            continue
        for row in rows:
            if row["url"] in taken:
                continue
            title = row["title"].lower()
            if any(pattern in title for pattern in patterns):
                chosen.append(row)
                taken.add(row["url"])
                filled.add(slot)
                break

    for row in rows:
        if len(chosen) >= 2:
            break
        if row["url"] not in taken:
            chosen.append(row)
            taken.add(row["url"])

    return chosen[:2]


def _sized(url: str) -> str:
    """Ask the CDN for an email-sized copy rather than the print-sized one."""
    if "cdn.shopify.com" not in url and "/cdn/shop/" not in url:
        return url
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}width={_CDN_WIDTH}"


def _download(url: str) -> tuple[bytes, str]:
    response = requests.get(_sized(url), timeout=_TIMEOUT, stream=True)
    response.raise_for_status()
    content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise ValueError(f"not an image: {content_type or 'unknown'}")

    # Read with a cap rather than trusting Content-Length, which an origin is
    # free to get wrong or omit.
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(64_000):
        total += len(chunk)
        if total > _MAX_BYTES_EACH:
            raise ValueError("image is too large for an email")
        chunks.append(chunk)
    return b"".join(chunks), content_type


def for_email(core: Any, handle: str) -> List[Dict[str, Any]]:
    """Photos ready to attach: title, bytes, subtype.

    Returns [] rather than raising. The caller is about to send an email and
    the pictures are the part it can do without.
    """
    handle = str(handle or "").strip().lower()
    if not handle:
        return []
    try:
        rows = _choose(_products(core, handle))
    except Exception as exc:
        print(f"[outreach-mockups] could not list products for {handle}: {exc}")
        return []

    photos: List[Dict[str, Any]] = []
    total = 0
    for row in rows:
        try:
            raw, content_type = _download(row["url"])
        except Exception as exc:
            print(f"[outreach-mockups] skipped a photo for {handle}: {exc}")
            continue
        if total + len(raw) > _MAX_BYTES_TOTAL:
            break
        total += len(raw)
        photos.append({
            "title": row["title"],
            "alt": row["alt"] or row["title"],
            "data": raw,
            "subtype": content_type.split("/", 1)[-1] or "jpeg",
        })
    return photos
