#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Copy every store's logo URL onto its metaobject as text. Run once.

WHY THIS EXISTS

Shopify resolves at most twenty metaobject FILE references per request. The
seller dashboard renders one card per store, and every card that resolves its
`logo` reference spends one of those twenty. Measured on a real account: cards
1-20 drew their logo and cards 21-36 drew nothing, on one page, with the break
falling between two alphabetically adjacent stores. Nothing in the theme can
raise that ceiling.

Reading a TEXT field costs nothing against the budget. Provisioning now writes
the logo's CDN URL alongside the file reference, so new stores are fine — this
backfills the ones created before that.

HOW TO RUN IT

From your own machine, NOT inside the Railway service:

    SHOP=<shop>.myshopify.com \\
    API_VERSION=2026-01 \\
    CLIENT_SECRET=<admin api token> \\
    python3 backfill_logo_urls.py --dry-run

Check what it reports, then run it again without --dry-run.

It only ever fills a field that is empty: a store that already has a URL is
left alone, so running it twice is safe and running it after a re-provision
cannot overwrite fresher data.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List

import shopify_provision as sp


LIST_QUERY = """
query Stores($type: String!, $cursor: String) {
  metaobjects(type: $type, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      handle
      fields { key value reference { ... on MediaImage { image { url } } } }
    }
  }
}
"""

UPDATE = """
mutation Fill($id: ID!, $fields: [MetaobjectFieldInput!]!) {
  metaobjectUpdate(id: $id, metaobject: { fields: $fields }) {
    userErrors { field message }
  }
}
"""


def all_stores() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor = None
    while True:
        data = sp.shopify_graphql(LIST_QUERY, {"type": sp.METAOBJECT_TYPE, "cursor": cursor})
        block = (data or {}).get("metaobjects") or {}
        out.extend(block.get("nodes") or [])
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return out
        cursor = page.get("endCursor")


def logo_url_of(node: Dict[str, Any]) -> str:
    """The URL behind this store's `logo` reference, if it has one."""
    for field in node.get("fields") or []:
        if (field.get("key") or "") != "logo":
            continue
        reference = field.get("reference") or {}
        image = reference.get("image") or {}
        return (image.get("url") or "").strip()
    return ""


def existing_text_url(node: Dict[str, Any], key: str) -> str:
    for field in node.get("fields") or []:
        if (field.get("key") or "") == key:
            return (field.get("value") or "").strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args()

    key = sp.logo_url_field()
    if not key:
        print(
            "STOP: the %s definition has no logo_url or logo_clean_url field.\n"
            "Add a single-line-text field named 'logo_url' to it in Shopify admin\n"
            "(Settings -> Custom data -> Metaobjects), then run this again."
            % sp.METAOBJECT_TYPE,
            file=sys.stderr,
        )
        return 2

    print("Writing to field: %s" % key)
    stores = all_stores()
    print("Found %d stores" % len(stores))

    filled = skipped_have = skipped_no_logo = failed = 0

    for node in stores:
        handle = node.get("handle") or "?"

        if existing_text_url(node, key):
            skipped_have += 1
            continue

        url = logo_url_of(node)
        if not url:
            print("  - %s: no logo reference to copy" % handle)
            skipped_no_logo += 1
            continue

        url = sp.logo_url_for_text(url)

        if args.dry_run:
            print("  would fill %s -> %s" % (handle, url))
            filled += 1
            continue

        try:
            data = sp.shopify_graphql(UPDATE, {
                "id": node["id"],
                "fields": [{"key": key, "value": url}],
            })
            errors = ((data or {}).get("metaobjectUpdate") or {}).get("userErrors") or []
            if errors:
                print("  ! %s: %s" % (handle, errors))
                failed += 1
                continue
        except Exception as exc:
            print("  ! %s: %s" % (handle, exc))
            failed += 1
            continue

        print("  filled %s" % handle)
        filled += 1

    print(
        "\n%s: %d filled, %d already had one, %d had no logo, %d failed"
        % ("Would change" if args.dry_run else "Done", filled, skipped_have, skipped_no_logo, failed)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
